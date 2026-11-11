"""H1 paper-forward funding seed: official archive module 3 + REST merge.

Data plumbing only. Does not change frozen strategy files. Swaps are never traded.
Parsing matches screening: funding_time / fundingTime are Unix epoch instants;
no extra +8h shift is applied.

Every zip is re-downloaded from the official source. The earlier local 2023-01
BTC probe is never reused.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from tradingbot.eval.v3_funding import (
    LIST_URL,
    REST_URL,
    STALE_CALENDAR_DAYS,
    UA,
    _read_archive_zip,
    crowding_flags,
    last_print_at_or_before,
    parse_archive_funding_time_ms,
)
from tradingbot.eval.v3_protocol import ts_saigon

INSTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
SEED_DIR = Path("/workspace/trading-bot-v3/data/funding/h1_paper_seed")
SEED_FILE = SEED_DIR / "h1_funding_seed.json"
NOTES_PATH = Path("/workspace/sprint/artifacts/spec_v3/freeze_notes_h1_seed.json")
# At least 365d before T0 (2026-11-12) plus margin, and include the 2023-01 BTC zip.
SEED_START_MONTH = "2023-01"
SAIGON = timezone(timedelta(hours=7))
UTC8 = timezone(timedelta(hours=8))


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def month_usable_at(month: str) -> pd.Timestamp:
    """A YYYY-MM archive zip is usable only after that UTC+8 month has closed.

    usable_at is the next month's 00:00 UTC+8, stored as a tz-naive UTC instant
    (same convention as parse_archive_funding_time_ms).
    """
    year, mon = map(int, month.split("-"))
    if mon == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=UTC8)
    else:
        nxt = datetime(year, mon + 1, 1, tzinfo=UTC8)
    utc = nxt.astimezone(timezone.utc)
    return pd.Timestamp(
        datetime(utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, utc.microsecond)
    )


def merge_seed_and_rest(
    archive: dict[str, pd.Series],
    rest: dict[str, pd.Series],
) -> dict[str, pd.Series]:
    """Concatenate archive + REST and de-duplicate by fundingTime. REST wins on a clash."""
    out: dict[str, pd.Series] = {}
    insts = set(archive) | set(rest)
    for inst in insts:
        parts = [s for s in (archive.get(inst), rest.get(inst)) if s is not None and len(s)]
        if not parts:
            out[inst] = pd.Series(dtype=float)
            continue
        s = pd.concat(parts)
        s = s[~s.index.duplicated(keep="last")].sort_index().astype(float)
        out[inst] = s
    return out


def archive_series_asof(
    chunks: list[dict],
    signal_time,
) -> dict[str, pd.Series]:
    """Keep only months already published / usable at signal_time (point-in-time)."""
    t = pd.Timestamp(signal_time)
    if t.tzinfo is not None:
        t = pd.Timestamp(
            datetime.fromtimestamp(t.timestamp(), tz=timezone.utc).replace(tzinfo=None)
        )
    acc: dict[str, list[pd.Series]] = {}
    for ch in chunks:
        usable = pd.Timestamp(ch["usable_at"])
        if usable > t:
            continue
        inst = ch["inst"]
        acc.setdefault(inst, []).append(ch["series"])
    out: dict[str, pd.Series] = {}
    for inst, parts in acc.items():
        s = pd.concat(parts)
        s = s[~s.index.duplicated(keep="last")].sort_index().astype(float)
        out[inst] = s
    return out


def crowd_for_paper(
    merged: dict[str, pd.Series],
    rest: dict[str, pd.Series],
    index: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, list[dict]]:
    """crowding_flags on the merged (365d) series; a missing/stale forward REST day turns the filter OFF.

    Archive is never used to backfill that day's rate.
    """
    events: list[dict] = []
    if len(index) == 0:
        return pd.DataFrame(), events
    crowd = crowding_flags(merged, index)
    for t in index:
        close_utc = pd.Timestamp(t) + pd.Timedelta(days=1)
        for side, inst, skip_col, crowd_col in (
            ("btc", "BTC-USDT-SWAP", "btc_skip", "btc_crowd"),
            ("eth", "ETH-USDT-SWAP", "eth_skip", "eth_crowd"),
        ):
            s = rest.get(inst, pd.Series(dtype=float))
            ts, rate = last_print_at_or_before(s, close_utc)
            missing = ts is None or rate is None
            stale = (not missing) and (close_utc - pd.Timestamp(ts)).days > STALE_CALENDAR_DAYS
            if missing or stale:
                if t in crowd.index:
                    crowd.at[t, skip_col] = True
                    crowd.at[t, crowd_col] = False
                events.append(
                    {
                        "type": "h1_funding_missing",
                        "action": "filter_OFF",
                        "reason": "rest_missing" if missing else "rest_stale",
                        "date": str(pd.Timestamp(t).date()),
                        "inst": inst,
                    }
                )
    return crowd, events


def trailing_window_stats(s: pd.Series, t_close, days: int = 365) -> dict:
    t_close = pd.Timestamp(t_close)
    requested_start = t_close - pd.Timedelta(days=days)
    if s is None or len(s) == 0:
        return {
            "requested_start": requested_start,
            "actual_start": None,
            "t_close": t_close,
            "n_prints": 0,
            "span_days": 0,
        }
    window = s.loc[requested_start:t_close]
    if window.empty:
        return {
            "requested_start": requested_start,
            "actual_start": None,
            "t_close": t_close,
            "n_prints": 0,
            "span_days": 0,
        }
    actual_start = pd.Timestamp(window.index[0])
    return {
        "requested_start": requested_start,
        "actual_start": actual_start,
        "actual_end": pd.Timestamp(window.index[-1]),
        "t_close": t_close,
        "n_prints": int(len(window)),
        "span_days": float((t_close - actual_start) / pd.Timedelta(days=1)),
    }


def _family(inst: str) -> str:
    return inst.replace("-SWAP", "")


def list_archive_months(
    session: requests.Session,
    inst: str,
    start: str,
    end: str,
) -> list[dict]:
    """List official module-3 monthly zips in <=10-month chunks. Never uses the local probe."""
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") + pd.offsets.MonthEnd(0)
    out: list[dict] = []
    cursor = start_ts
    last_list = 0.0
    while cursor <= end_ts:
        chunk_end = min(cursor + pd.DateOffset(months=9), end_ts)
        begin_ms = int(cursor.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        wait = 0.45 - (time.time() - last_list)
        if wait > 0:
            time.sleep(wait)
        r = session.get(
            LIST_URL,
            params={
                "module": "3",
                "instType": "SWAP",
                "instFamilyList": _family(inst),
                "dateAggrType": "monthly",
                "begin": begin_ms,
                "end": end_ms,
            },
            headers=UA,
            timeout=30,
        )
        last_list = time.time()
        if r.status_code == 403:
            raise RuntimeError(f"403 listing funding archive for {inst}; stopping (will not change User-Agent).")
        r.raise_for_status()
        payload = r.json()
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"archive list error {payload.get('code')} {payload.get('msg')}")
        for block in payload.get("data") or []:
            for det in block.get("details") or []:
                for g in det.get("groupDetails") or []:
                    fname = g.get("filename") or ""
                    url = g.get("url") or ""
                    month = None
                    if "fundingrates-" in fname:
                        month = fname.split("fundingrates-")[-1].replace(".zip", "")
                    if not month or not url:
                        continue
                    if month < start[:7] or month > end[:7]:
                        # list chunks can overflow a month on the end boundary
                        if month < SEED_START_MONTH:
                            continue
                    out.append(
                        {
                            "inst": inst,
                            "month": month,
                            "url": url,
                            "filename": fname,
                            "dateTs": g.get("dateTs"),
                            "usable_at": str(month_usable_at(month)),
                            "sizeMB_listed": g.get("sizeMB"),
                        }
                    )
        cursor = chunk_end + pd.DateOffset(days=1)
    # de-dupe (inst, month) keeping first
    seen = set()
    uniq = []
    for rec in out:
        key = (rec["inst"], rec["month"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(rec)
    return uniq


def download_zip(session: requests.Session, url: str, dest: Path) -> None:
    """Always fetch from the official URL. Never copy a local probe or an older cache zip."""
    r = session.get(url, headers=UA, timeout=60)
    if r.status_code == 403:
        raise RuntimeError(f"403 downloading {url}; stopping (will not change User-Agent).")
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)


def series_from_zip(path: Path, inst: str) -> pd.Series:
    rows = _read_archive_zip(path.read_bytes())
    pairs = [(rec["ts_utc"], rec["rate"]) for rec in rows if rec.get("inst") in (inst, inst.replace("-SWAP", ""))]
    if not pairs:
        # some csvs use the full inst id only
        pairs = [(rec["ts_utc"], rec["rate"]) for rec in rows]
    if not pairs:
        return pd.Series(dtype=float)
    s = pd.Series({t: r for t, r in pairs}).sort_index()
    return s[~s.index.duplicated(keep="last")].astype(float)


def _series_to_pairs(s: pd.Series) -> list[list]:
    return [[int(pd.Timestamp(t).timestamp() * 1000), float(v)] for t, v in s.items()]


def _pairs_to_series(pairs: list) -> pd.Series:
    if not pairs:
        return pd.Series(dtype=float)
    s = pd.Series({parse_archive_funding_time_ms(int(ms)): float(rate) for ms, rate in pairs}).sort_index()
    return s[~s.index.duplicated(keep="last")].astype(float)


def write_seed_file(chunks: list[dict], path: Path = SEED_FILE) -> Path:
    payload = {
        "parse": "funding_time treated as Unix epoch instant; no extra +8h shift",
        "source": "OKX official archive module 3 (re-downloaded; local probe not used)",
        "chunks": [
            {
                "inst": ch["inst"],
                "month": ch["month"],
                "usable_at": str(ch["usable_at"]),
                "url": ch.get("url"),
                "sha256": ch.get("sha256"),
                "n_prints": int(len(ch["series"])),
                "pairs": _series_to_pairs(ch["series"]),
            }
            for ch in chunks
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str))
    return path


def load_seed_chunks(path: Path = SEED_FILE) -> list[dict]:
    raw = json.loads(Path(path).read_text())
    chunks = []
    for ch in raw.get("chunks") or []:
        chunks.append(
            {
                "inst": ch["inst"],
                "month": ch["month"],
                "usable_at": pd.Timestamp(ch["usable_at"]),
                "url": ch.get("url"),
                "sha256": ch.get("sha256"),
                "series": _pairs_to_series(ch.get("pairs") or []),
            }
        )
    return chunks


def seed_archive(
    *,
    dest_dir: Path = SEED_DIR,
    start: str = "2023-01-01",
    end: str | None = None,
    insts: tuple[str, ...] = INSTS,
    notes_path: Path = NOTES_PATH,
    session: requests.Session | None = None,
) -> dict:
    """Re-download every needed official module-3 zip (including 2023-01 BTC) and write notes."""
    if end is None:
        # Latest completed UTC+8 month as of now (do not invent a current unfinished month).
        now_utc8 = datetime.now(timezone.utc).astimezone(UTC8)
        first_this = datetime(now_utc8.year, now_utc8.month, 1, tzinfo=UTC8)
        last_completed = first_this - timedelta(days=1)
        end = last_completed.strftime("%Y-%m-%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    listed: list[dict] = []
    for inst in insts:
        listed.extend(list_archive_months(session, inst, start, end))
    records = []
    chunks: list[dict] = []
    errors: list[dict] = []
    # Always re-download. Never copy LOCAL_PROBES or screening zips.
    for rec in listed:
        dest = dest_dir / f"{rec['inst']}-fundingrates-{rec['month']}.zip"
        downloaded_at = ts_saigon()
        try:
            time.sleep(0.2)
            download_zip(session, rec["url"], dest)
            digest = sha256_file(dest)
            series = series_from_zip(dest, rec["inst"])
            rec_out = {
                **rec,
                "path": str(dest),
                "sha256": digest,
                "bytes": dest.stat().st_size,
                "downloaded_at": downloaded_at,
                "source": "official_archive_redownload",
                "local_probe_reused": False,
                "n_prints": int(len(series)),
            }
            records.append(rec_out)
            chunks.append(
                {
                    "inst": rec["inst"],
                    "month": rec["month"],
                    "usable_at": rec["usable_at"],
                    "url": rec["url"],
                    "sha256": digest,
                    "series": series,
                }
            )
            print(
                f"seed {rec['inst']} {rec['month']}: downloaded {dest.stat().st_size} bytes sha256={digest[:12]}… n={len(series)}",
                flush=True,
            )
        except Exception as e:
            errors.append({"inst": rec["inst"], "month": rec["month"], "url": rec["url"], "error": str(e)})
            print(f"seed {rec['inst']} {rec['month']}: ERROR {e}", flush=True)
            if "403" in str(e):
                raise
    seed_path = write_seed_file(chunks, dest_dir / "h1_funding_seed.json")
    seed_sha = sha256_file(seed_path)
    # Build a current as-of-now series (all months usable now) for the notes window check.
    from tradingbot.eval.v3_paper import T0_BAR

    t0_close = T0_BAR + pd.Timedelta(days=1)
    now_series = archive_series_asof(chunks, t0_close)
    window = {
        inst: trailing_window_stats(now_series.get(inst, pd.Series(dtype=float)), t0_close, days=365)
        for inst in insts
    }
    notes = {
        "created_at": ts_saigon(),
        "purpose": "H1 paper seed so trailing_p90 at T0 uses a ~365d official window, not the REST-only ~90d span. Cypheous option A. Not a rewrite of freeze.json.",
        "sha256_v3_code_unchanged": "533ee52ed5945f0324bdfe1dd37f172684ba732f4da5fd31b2260e15c2d691bb",
        "source": "OKX official archive module 3 (GET /api/v5/public/market-data-history module=3) + later REST funding-rate-history merge at runtime",
        "parse": "archive funding_time and REST fundingTime are Unix epoch instants; no extra +8h shift",
        "local_probe_reused": False,
        "2023_01_BTC": "re-downloaded from the official source; earlier /workspace/crypto probe was not used",
        "seed_start": start,
        "seed_end": end,
        "insts": list(insts),
        "n_zips": len(records),
        "zips": [
            {
                "inst": r["inst"],
                "month": r["month"],
                "url": r["url"],
                "sha256": r["sha256"],
                "bytes": r["bytes"],
                "downloaded_at": r["downloaded_at"],
                "usable_at": r["usable_at"],
                "n_prints": r["n_prints"],
            }
            for r in records
        ],
        "seed_file": {
            "path": str(seed_path),
            "sha256": seed_sha,
            "n_prints": {inst: int(len(now_series.get(inst, []))) for inst in insts},
            "first": {inst: (str(now_series[inst].index[0]) if inst in now_series and len(now_series[inst]) else None) for inst in insts},
            "last": {inst: (str(now_series[inst].index[-1]) if inst in now_series and len(now_series[inst]) else None) for inst in insts},
        },
        "t0_window_check_archive_only": {
            inst: {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in stats.items()}
            for inst, stats in window.items()
        },
        "errors": errors,
        "list_url": LIST_URL,
        "rest_url": REST_URL,
    }
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(json.dumps(notes, indent=2, default=str))
    # Convenience copy in the v3 artifacts folder (not freeze.json).
    art_copy = Path("/workspace/trading-bot-v3/artifacts/spec_v3/freeze_notes_h1_seed.json")
    art_copy.parent.mkdir(parents=True, exist_ok=True)
    art_copy.write_text(notes_path.read_text())
    print(f"wrote notes {notes_path} zips={len(records)} errors={len(errors)} seed_sha={seed_sha}", flush=True)
    return notes


if __name__ == "__main__":
    seed_archive()
