"""H1 funding history: OKX public archive module 3 + REST (signal only; never trade swaps).

Source policy (strategy-v3-brief §H1 / Soozie):
- REST `GET /api/v5/public/funding-rate-history` has only ~3 months (limit 400).
- Longer history comes from official archive module 3 (funding rate).
- Archive module 3 *file months* are UTC+8 (Soozie / brief). `funding_time` itself is a Unix
  epoch instant (the 2023-01 zip starts at 2023-01-01 00:00 UTC+8 = 2022-12-31 16:00 UTC).
  REST `fundingTime` uses the same instant convention. We do not apply an extra 8h shift.
- The 2023-01 BTC zip already probed under /workspace/crypto/reversal-v2/raw/funding/
  is copied, not re-downloaded.
- If a listing or zip fetch returns 403, we stop (do not change User-Agent and retry).
- Stale >3 calendar days: callers must skip the filter (do not invent a rate).
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

FUNDING_DIR = Path("/workspace/trading-bot-v3/data/funding")
ARTIFACT = Path("/workspace/sprint/artifacts/spec_v3")
LIST_URL = "https://www.okx.com/api/v5/public/market-data-history"
REST_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
UA = {"User-Agent": "Mozilla/5.0"}
# Already probed; never re-download.
LOCAL_PROBES = {
    ("BTC-USDT-SWAP", "2023-01"): Path(
        "/workspace/crypto/reversal-v2/raw/funding/BTC_2023-01.zip"
    ),
}
INSTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
STALE_CALENDAR_DAYS = 3


def _family(inst: str) -> str:
    return inst.replace("-SWAP", "")


def _naive_utc(dt: datetime) -> pd.Timestamp:
    """Return a tz-naive pandas Timestamp in UTC wall clock, independent of the box TZ."""
    if dt.tzinfo is None:
        raise ValueError("expected aware datetime")
    utc = dt.astimezone(timezone.utc)
    return pd.Timestamp(datetime(utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, utc.microsecond))


def parse_archive_funding_time_ms(ms: int) -> pd.Timestamp:
    """Archive `funding_time` is a Unix epoch instant (ms).

    Soozie/brief: module 3 *month buckets* are UTC+8. The probed 2023-01 zip starts at
    1672502400000 = 2022-12-31 16:00 UTC = 2023-01-01 00:00 UTC+8. We do **not**
    subtract another 8 hours, which would mis-align prints against 1Dutc closes.
    Same instant convention as REST `fundingTime`.
    """
    return _naive_utc(datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc))


def parse_rest_funding_time_ms(ms: int) -> pd.Timestamp:
    """REST `fundingTime` is Unix epoch ms (UTC instant). Same convention as the archive."""
    return _naive_utc(datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc))



def _read_archive_zip(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        text = zf.read(name).decode("utf-8")
    rows = []
    lines = text.splitlines()
    if not lines:
        return rows
    header = [h.strip() for h in lines[0].split(",")]
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        rec = dict(zip(header, parts))
        inst = rec.get("instrument_name") or rec.get("instId") or rec.get("instrument")
        rate = rec.get("funding_rate") or rec.get("fundingRate")
        ts = rec.get("funding_time") or rec.get("fundingTime")
        if inst is None or rate is None or ts is None:
            continue
        rows.append(
            {
                "inst": inst,
                "rate": float(rate),
                "ts_raw_ms": int(float(ts)),
                "ts_utc": parse_archive_funding_time_ms(int(float(ts))),
                "source": "archive_module3",
            }
        )
    return rows


def _copy_local_probe(inst: str, month: str, dest_dir: Path) -> Path | None:
    src = LOCAL_PROBES.get((inst, month))
    if src is None or not src.exists():
        return None
    dest = dest_dir / f"{inst}-fundingrates-{month}.zip"
    if not dest.exists():
        dest.write_bytes(src.read_bytes())
    return dest


def _list_months(session: requests.Session, inst: str, begin_ms: int, end_ms: int) -> list[dict]:
    fam = _family(inst)
    r = session.get(
        LIST_URL,
        params={
            "module": "3",
            "instType": "SWAP",
            "instFamilyList": fam,
            "dateAggrType": "monthly",
            "begin": begin_ms,
            "end": end_ms,
        },
        headers=UA,
        timeout=30,
    )
    if r.status_code == 403:
        raise RuntimeError(f"403 listing funding archive for {inst}; stopping (will not change User-Agent).")
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"archive list error {payload.get('code')} {payload.get('msg')}")
    out = []
    for block in payload.get("data") or []:
        for det in block.get("details") or []:
            for g in det.get("groupDetails") or []:
                out.append(g)
    return out


def _download_zip(session: requests.Session, url: str, dest: Path) -> None:
    r = session.get(url, headers=UA, timeout=60)
    if r.status_code == 403:
        raise RuntimeError(f"403 downloading {url}; stopping (will not change User-Agent).")
    r.raise_for_status()
    dest.write_bytes(r.content)


def fetch_archive(
    dest_dir: Path = FUNDING_DIR,
    start: str = "2022-01-01",
    end: str = "2024-12-31",
    insts: tuple[str, ...] = INSTS,
) -> dict:
    """List and download monthly module-3 zips. Reuses the probed 2023-01 BTC zip."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") + pd.offsets.MonthEnd(0)
    # API max range 10 months per list call
    records = []
    errors = []
    last_list = 0.0
    for inst in insts:
        cursor = start_ts
        while cursor <= end_ts:
            chunk_end = min(cursor + pd.DateOffset(months=9), end_ts)
            begin_ms = int(cursor.timestamp() * 1000)
            end_ms = int(chunk_end.timestamp() * 1000)
            wait = 0.45 - (time.time() - last_list)
            if wait > 0:
                time.sleep(wait)
            try:
                details = _list_months(session, inst, begin_ms, end_ms)
                last_list = time.time()
            except Exception as e:
                errors.append({"inst": inst, "begin": str(cursor.date()), "error": str(e)})
                break
            for g in details:
                fname = g.get("filename") or ""
                url = g.get("url") or ""
                # filename like BTC-USDT-SWAP-fundingrates-2023-01.zip
                month = None
                if "fundingrates-" in fname:
                    month = fname.split("fundingrates-")[-1].replace(".zip", "")
                if not month:
                    continue
                dest = dest_dir / f"{inst}-fundingrates-{month}.zip"
                if (inst, month) in LOCAL_PROBES:
                    copied = _copy_local_probe(inst, month, dest_dir)
                    records.append(
                        {
                            "inst": inst,
                            "month": month,
                            "path": str(copied or dest),
                            "source": "local_probe_copy",
                            "url": url,
                            "sizeMB_listed": g.get("sizeMB"),
                        }
                    )
                    print(f"funding {inst} {month}: reused local probe, not re-downloaded", flush=True)
                    continue
                if dest.exists() and dest.stat().st_size > 0:
                    records.append(
                        {
                            "inst": inst,
                            "month": month,
                            "path": str(dest),
                            "source": "already_on_disk",
                            "url": url,
                            "sizeMB_listed": g.get("sizeMB"),
                        }
                    )
                    print(f"funding {inst} {month}: already on disk", flush=True)
                    continue
                try:
                    time.sleep(0.2)
                    _download_zip(session, url, dest)
                    records.append(
                        {
                            "inst": inst,
                            "month": month,
                            "path": str(dest),
                            "source": "archive_download",
                            "url": url,
                            "bytes": dest.stat().st_size,
                            "sizeMB_listed": g.get("sizeMB"),
                        }
                    )
                    print(f"funding {inst} {month}: downloaded {dest.stat().st_size} bytes", flush=True)
                except Exception as e:
                    errors.append({"inst": inst, "month": month, "url": url, "error": str(e)})
                    print(f"funding {inst} {month}: ERROR {e}", flush=True)
                    if "403" in str(e):
                        raise
            cursor = chunk_end + pd.DateOffset(days=1)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start": start,
        "end": end,
        "insts": list(insts),
        "parse": "archive funding_time treated as UTC+8 wall clock; REST fundingTime as UTC instant",
        "records": records,
        "errors": errors,
    }
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "download_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    art = ARTIFACT / "data"
    art.mkdir(parents=True, exist_ok=True)
    (art / "funding_download_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str)
    )
    return manifest


def fetch_rest_recent(insts: tuple[str, ...] = INSTS, dest_dir: Path = FUNDING_DIR) -> dict[str, list]:
    """Recent ~3 months from public REST (no auth). Stored for documentation, not used to fill archive gaps in 2023-24."""
    session = requests.Session()
    out: dict[str, list] = {}
    for inst in insts:
        rows = []
        after = None
        for _ in range(20):
            params = {"instId": inst, "limit": 400}
            if after is not None:
                params["after"] = after
            time.sleep(0.25)
            r = session.get(REST_URL, params=params, headers=UA, timeout=20)
            if r.status_code == 403:
                raise RuntimeError(f"403 REST funding-rate-history for {inst}; stopping.")
            payload = r.json()
            if str(payload.get("code")) != "0" or not payload.get("data"):
                break
            batch = payload["data"]
            rows.extend(batch)
            after = batch[-1]["fundingTime"]
            if len(batch) < 400:
                break
        out[inst] = rows
        (dest_dir / f"{inst}-rest.json").write_text(json.dumps(rows))
        print(f"REST funding {inst}: {len(rows)} prints", flush=True)
    return out


def load_archive_series(dest_dir: Path = FUNDING_DIR) -> dict[str, pd.Series]:
    """inst → Series of funding rate indexed by UTC instant (archive, UTC+8 parsed)."""
    series: dict[str, list] = {i: [] for i in INSTS}
    for path in sorted(dest_dir.glob("*-fundingrates-*.zip")):
        rows = _read_archive_zip(path.read_bytes())
        for rec in rows:
            inst = rec["inst"]
            series.setdefault(inst, []).append((rec["ts_utc"], rec["rate"]))
    out = {}
    for inst, pairs in series.items():
        if not pairs:
            continue
        s = pd.Series({t: r for t, r in pairs}).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        out[inst] = s.astype(float)
    return out


def last_print_at_or_before(s: pd.Series, t_close: pd.Timestamp) -> tuple[pd.Timestamp | None, float | None]:
    t_close = pd.Timestamp(t_close)
    if t_close.tzinfo is not None:
        t_close = t_close.tz_convert("UTC").tz_localize(None)
    if s is None or s.empty:
        return None, None
    window = s.loc[:t_close]
    if window.empty:
        return None, None
    return pd.Timestamp(window.index[-1]), float(window.iloc[-1])


def trailing_p90(s: pd.Series, t_close: pd.Timestamp, days: int = 365, min_periods: int = 90) -> float | None:
    t_close = pd.Timestamp(t_close)
    start = t_close - pd.Timedelta(days=days)
    window = s.loc[start:t_close]
    if len(window) < min_periods:
        return None
    return float(window.quantile(0.90))


def crowding_flags(
    funding: dict[str, pd.Series],
    index: pd.DatetimeIndex,
    *,
    # 1Dutc index is bar OPEN date; close instant is open + 1 day (00:00 UTC).
    bar="1Dutc",
) -> pd.DataFrame:
    """For each 1Dutc open date, flags at that bar's close.

    Columns: btc_rate, eth_rate, btc_ts, eth_ts, btc_p90, eth_p90, btc_stale, eth_stale,
    btc_crowd (positive and >= p90), eth_crowd, btc_skip (stale → skip filter), eth_skip.
    """
    btc = funding.get("BTC-USDT-SWAP", pd.Series(dtype=float))
    eth = funding.get("ETH-USDT-SWAP", pd.Series(dtype=float))
    rows = []
    for dt in index:
        close_utc = pd.Timestamp(dt) + pd.Timedelta(days=1)
        b_ts, b_rate = last_print_at_or_before(btc, close_utc)
        e_ts, e_rate = last_print_at_or_before(eth, close_utc)
        b_stale = b_ts is None or (close_utc - pd.Timestamp(b_ts)).days > STALE_CALENDAR_DAYS
        e_stale = e_ts is None or (close_utc - pd.Timestamp(e_ts)).days > STALE_CALENDAR_DAYS
        b_p90 = trailing_p90(btc, close_utc) if not b_stale else None
        e_p90 = trailing_p90(eth, close_utc) if not e_stale else None
        b_crowd = (
            (not b_stale)
            and b_rate is not None
            and b_p90 is not None
            and b_rate > 0
            and b_rate >= b_p90
        )
        e_crowd = (
            (not e_stale)
            and e_rate is not None
            and e_p90 is not None
            and e_rate > 0
            and e_rate >= e_p90
        )
        rows.append(
            {
                "btc_rate": b_rate,
                "eth_rate": e_rate,
                "btc_ts": None if b_ts is None else str(b_ts),
                "eth_ts": None if e_ts is None else str(e_ts),
                "btc_p90": b_p90,
                "eth_p90": e_p90,
                "btc_stale": bool(b_stale),
                "eth_stale": bool(e_stale),
                "btc_crowd": bool(b_crowd),
                "eth_crowd": bool(e_crowd),
                "btc_skip": bool(b_stale),
                "eth_skip": bool(e_stale),
            }
        )
    return pd.DataFrame(rows, index=index)


if __name__ == "__main__":
    man = fetch_archive()
    print("archive months:", len(man["records"]), "errors:", len(man["errors"]))
    try:
        fetch_rest_recent()
    except Exception as e:
        print("REST recent failed:", e)
