"""Official spec v3 paper-forward loop (H5, H1, H4 + frozen V-C + 50/50 bench).

Cypheous signed off on these as FORWARD PAPER slots, not a strategy PASS.
Paper / dry-run only. No keys, no live orders, never the 6,788 USDT wedding funds.
C_paper=10000 only. Strategy code and H1/H4/H5 params are locked to freeze.json;
this module is runner plumbing and must not change frozen strategy logic.

H1 funding in this runner comes only from the public REST
`/api/v5/public/funding-rate-history` endpoint, as of signal time. If a day's
funding is missing, the H1 filter is OFF that day, the event is logged, and
we never backfill from the archive or any other source.

Bars before T0 (2026-11-12 07:00 UTC+7) are not counted toward results.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from tradingbot.config import Config
from tradingbot.eval.events import calendar_is_stale, last_fetch_timestamp, load_events
from tradingbot.eval.regime import build_regime
from tradingbot.eval.v2_engine import BookState, new_book, step_bench
from tradingbot.eval.v2_paper import _prepare_v1, _step_zara
from tradingbot.eval.v3_funding import (
    REST_URL,
    crowding_flags,
    parse_rest_funding_time_ms,
)
from tradingbot.eval.v3_hypotheses import build_hypotheses, params_for
from tradingbot.eval.v3_load import load_panel as v3_load_panel
from tradingbot.eval.v3_protocol import sha256_params, sha256_paths, ts_saigon
from tradingbot.strategies import spec_v2_common as P
from tradingbot.universe import inst_id, load_favorites

SAIGON = ZoneInfo("Asia/Saigon")
T0_BAR = pd.Timestamp(P.T0_BAR_OPEN_UTC.replace("Z", ""))  # 2026-11-11
T0_LABEL = "2026-11-12 07:00 UTC+7 (1Dutc close)"
OFFICIAL_RUN_ID = "spec_v3_official"
V3_ROOT = Path("/workspace/trading-bot-v3")
V3_CACHE = V3_ROOT / "data" / "cache"
ARTIFACT_ROOT = Path("/workspace/sprint/artifacts/spec_v3")
FREEZE_PATH = ARTIFACT_ROOT / "freeze.json"
LOCK_PATH = V3_ROOT / "RUNNING.lock"
REST_FUNDING_URL = REST_URL
BOOK_ORDER = ("H5", "H1", "H4", "V-C", "BENCH-50-50")
C_PAPER_OFFICIAL = 10000.0
WEDDING = 6788.0
COST = P.COST_2X  # 40 bps/side, 2x


class FreezeMismatch(RuntimeError):
    """Frozen v3 code or slotted params do not match freeze.json. Refuse to start."""


def run_dirs(run_id: str) -> dict[str, Path]:
    state = V3_ROOT / "state" / "spec_v3" / run_id
    logs = V3_ROOT / "logs" / "spec_v3" / run_id
    art = ARTIFACT_ROOT / run_id
    for p in (state, logs, art):
        p.mkdir(parents=True, exist_ok=True)
    return {
        "state": state,
        "logs": logs,
        "art": art,
        "ledger": state / "ledger.json",
        "pidfile": state / "paper.pid",
        "events": logs / "paper_events.jsonl",
        "funding_rest": state / "funding_rest.json",
    }


def c_paper_official(cfg: Config) -> float:
    c = getattr(cfg, "c_paper", None)
    if c is None:
        raise ValueError("C_paper is required for official v3 paper; never inferred from an OKX account.")
    c = float(c)
    if abs(c - WEDDING) < 1e-9:
        raise ValueError("C_paper equals the excluded 6,788 USDT wedding funds; refuse.")
    if abs(c - C_PAPER_OFFICIAL) > 1e-9:
        raise ValueError("Official v3 paper uses C_paper=10000 only.")
    return C_PAPER_OFFICIAL


def verify_freeze(path: Path | str = FREEZE_PATH) -> dict:
    freeze = json.loads(Path(path).read_text())
    code = sha256_paths()
    expected_code = freeze["sha256_v3_code"]
    if code != expected_code:
        raise FreezeMismatch(f"code hash mismatch: got {code} expected {expected_code}")
    params: dict[str, str] = {}
    for hid in ("H1", "H4", "H5"):
        got = sha256_params(params_for(hid))
        exp = next(c["sha256_params"] for c in freeze["candidates"] if c["id"] == hid)
        if got != exp:
            raise FreezeMismatch(f"{hid} params hash mismatch: got {got} expected {exp}")
        params[hid] = got
    return {
        "ok": True,
        "sha256_brief": freeze["sha256_brief"],
        "sha256_v3_code": code,
        "params": params,
        "forward_slots": list(freeze.get("forward_slots") or ["H5", "H1", "H4"]),
        "frozen_at": freeze.get("frozen_at"),
        "freeze_path": str(Path(path)),
    }


def format_hash_verification(info: dict) -> str:
    lines = [
        "v3 freeze verification: OK",
        f"  sha256_brief    {info['sha256_brief']}",
        f"  sha256_v3_code  {info['sha256_v3_code']}",
        f"  H1 params       {info['params']['H1']}",
        f"  H4 params       {info['params']['H4']}",
        f"  H5 params       {info['params']['H5']}",
        f"  forward_slots   {info['forward_slots']}",
        f"  frozen_at       {info.get('frozen_at')}",
    ]
    return "\n".join(lines)


def running_lock_text(*, pid: int, started: str) -> str:
    return "\n".join(
        [
            "owner: Tobias - Trading Engineer",
            f"pid: {pid}",
            "process: .venv/bin/python -m tradingbot paper-start --strategies v3 --run-id spec_v3_official --poll 60",
            "purpose: official spec v3 paper-forward slots H5, H1, H4 + frozen V-C comparator + 50/50 BTC/ETH bench (paper only, no keys, no live). Cypheous signed off on paper slots, not a strategy PASS.",
            f"started: {started}",
            f"t0: {T0_LABEL}",
            "c_paper: 10000 (never the 6,788 USDT wedding funds; never an OKX account balance)",
            "rule: do NOT copy, re-clone, git clean/reset, or delete .venv/state/logs/data in this folder while this file exists. Do not touch /workspace/trading-bot (pid 129703), D1, the Cloudflare Worker, or the live-ready worktree.",
            "",
        ]
    )


def write_running_lock(*, pid: int, started: str | None = None, path: Path = LOCK_PATH) -> Path:
    started = started or ts_saigon()
    path.write_text(running_lock_text(pid=pid, started=started))
    return path


def clear_running_lock(pid: int | None = None, path: Path = LOCK_PATH) -> None:
    if not path.exists():
        return
    if pid is not None:
        try:
            text = path.read_text()
        except OSError:
            return
        if f"pid: {pid}" not in text:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def fetch_rest_funding_series(
    *,
    http_get=None,
    dest_dir: Path | str | None = None,
    insts: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
    session=None,
) -> tuple[dict[str, pd.Series], list[dict]]:
    """Public REST funding-rate-history only. Never reads archive zips. Never backfills."""
    getter = http_get or (session.get if session is not None else requests.get)
    events: list[dict] = []
    out: dict[str, pd.Series] = {}
    raw: dict[str, list] = {}
    headers = {"User-Agent": "Mozilla/5.0 (research; paper-only; no-auth)"}
    for inst in insts:
        rows: list[dict] = []
        after = None
        try:
            for _ in range(20):
                params = {"instId": inst, "limit": 400}
                if after is not None:
                    params["after"] = after
                r = getter(REST_FUNDING_URL, params=params, headers=headers, timeout=20)
                if getattr(r, "status_code", 200) == 403:
                    events.append(
                        {
                            "type": "h1_funding_missing",
                            "action": "filter_OFF",
                            "reason": "rest_403",
                            "inst": inst,
                        }
                    )
                    rows = []
                    break
                payload = r.json()
                if str(payload.get("code")) != "0" or not payload.get("data"):
                    break
                batch = payload["data"]
                rows.extend(batch)
                after = batch[-1]["fundingTime"]
                if len(batch) < 400:
                    break
        except Exception as e:
            events.append(
                {
                    "type": "h1_funding_missing",
                    "action": "filter_OFF",
                    "reason": f"REST error: {e}",
                    "inst": inst,
                }
            )
            out[inst] = pd.Series(dtype=float)
            raw[inst] = []
            continue
        raw[inst] = rows
        pairs = []
        for rec in rows:
            ts_raw = rec.get("fundingTime") or rec.get("funding_time")
            rate_raw = rec.get("fundingRate") or rec.get("funding_rate")
            if ts_raw is None or rate_raw is None:
                continue
            pairs.append((parse_rest_funding_time_ms(int(ts_raw)), float(rate_raw)))
        if pairs:
            s = pd.Series({t: r for t, r in pairs}).sort_index()
            s = s[~s.index.duplicated(keep="last")].astype(float)
            out[inst] = s
        else:
            out[inst] = pd.Series(dtype=float)
            events.append(
                {
                    "type": "h1_funding_missing",
                    "action": "filter_OFF",
                    "reason": "rest_empty",
                    "inst": inst,
                }
            )
    if dest_dir is not None:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        # Snapshot of the REST payload only — never an archive zip, never a backfill.
        (dest / "funding_rest_snapshot.json").write_text(
            json.dumps({"source": REST_FUNDING_URL, "n": {k: len(v) for k, v in raw.items()}}, default=str)
        )
    return out, events


def crowd_from_rest_or_off(
    funding: dict[str, pd.Series],
    index: pd.DatetimeIndex,
    reason: str = "rest_empty",
) -> tuple[pd.DataFrame, list[dict]]:
    """Build H1 crowding flags from REST series only. Missing day → filter OFF + event."""
    events: list[dict] = []
    empty = (not funding) or all((s is None) or (len(s) == 0) for s in funding.values())
    if empty or len(index) == 0:
        events.append(
            {
                "type": "h1_funding_missing",
                "action": "filter_OFF",
                "reason": reason,
                "scope": "all",
            }
        )
        return pd.DataFrame(), events
    crowd = crowding_flags(funding, index)
    for t in index:
        if t not in crowd.index:
            events.append(
                {
                    "type": "h1_funding_missing",
                    "action": "filter_OFF",
                    "reason": "day_absent",
                    "date": str(pd.Timestamp(t).date()),
                }
            )
            continue
        row = crowd.loc[t]
        b_rate = row.get("btc_rate")
        e_rate = row.get("eth_rate")
        b_missing = b_rate is None or (isinstance(b_rate, float) and pd.isna(b_rate))
        e_missing = e_rate is None or (isinstance(e_rate, float) and pd.isna(e_rate))
        if b_missing:
            crowd.at[t, "btc_skip"] = True
            crowd.at[t, "btc_crowd"] = False
            events.append(
                {
                    "type": "h1_funding_missing",
                    "action": "filter_OFF",
                    "reason": "btc_missing",
                    "date": str(pd.Timestamp(t).date()),
                    "inst": "BTC-USDT-SWAP",
                }
            )
        if e_missing:
            crowd.at[t, "eth_skip"] = True
            crowd.at[t, "eth_crowd"] = False
            events.append(
                {
                    "type": "h1_funding_missing",
                    "action": "filter_OFF",
                    "reason": "eth_missing",
                    "date": str(pd.Timestamp(t).date()),
                    "inst": "ETH-USDT-SWAP",
                }
            )
    return crowd, events


def _last_closed_open(panel: dict) -> pd.Timestamp | None:
    idx = panel["close"].index
    return None if len(idx) == 0 else pd.Timestamp(idx[-1])


def _append_events(path: Path, events: list[dict]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for ev in events:
            rec = dict(ev)
            rec.setdefault("ts", ts_saigon())
            f.write(json.dumps(rec, default=str) + "\n")


def _empty_book_pack() -> dict:
    return {
        "equity": None,
        "positions": {},
        "maxDD": None,
        "n_closed": 0,
        "n_counted_bars": 0,
        "killed": None,
    }


def _pack_book(st: BookState | None) -> dict:
    if st is None:
        return _empty_book_pack()
    eq = st.equity
    peak = st.peak or eq
    dd = (eq / peak - 1.0) if peak else 0.0
    n_counted = len(st.equity_curve or [])
    if st.equity_curve:
        ser = pd.Series([r["equity"] for r in st.equity_curve])
        dd = float((ser / ser.cummax() - 1.0).min()) if len(ser) else dd
    # Uninitialized / waiting books have capital in cash but no counted bars.
    if n_counted == 0 and not any(abs(float(q)) > 1e-12 for q in (st.qty or {}).values()):
        return {
            "equity": None,
            "cash": st.cash,
            "positions": {},
            "maxDD": None,
            "n_closed": 0,
            "n_counted_bars": 0,
            "killed": st.killed,
            "killed_on": st.killed_on,
            "last_processed": st.last_processed,
        }
    pos = {s: q for s, q in st.qty.items() if abs(float(q)) > 1e-12}
    return {
        "equity": st.equity,
        "cash": st.cash,
        "positions": pos,
        "maxDD": dd,
        "n_closed": len(st.trades_closed),
        "n_open": len(st.trades_open),
        "n_counted_bars": n_counted,
        "killed": st.killed,
        "killed_on": st.killed_on,
        "last_processed": st.last_processed,
    }


def _section7(
    *,
    run_id: str,
    official: bool,
    waiting: bool,
    initialized: bool,
    last_closed: str | None,
    regime: str,
    books: dict[str, BookState] | dict,
    errors: list[str],
    calendar_ok: bool,
    notes: list[str],
    hashes: dict | None,
) -> dict:
    packed = {}
    for name in BOOK_ORDER:
        raw = books.get(name) if isinstance(books, dict) else None
        packed[name] = _pack_book(raw) if isinstance(raw, BookState) else (raw or _empty_book_pack())
    return {
        "run_id": run_id,
        "official": official,
        "label": "OFFICIAL spec v3 paper-forward (paper slots; not a strategy PASS)",
        "waiting_for_t0": waiting,
        "t0_initialized": bool(initialized) and not waiting,
        "t0": T0_LABEL,
        "last_closed_1dutc_open": last_closed,
        "as_of": ts_saigon(),
        "regime_state": regime,
        "errors": errors,
        "n_errors": len(errors),
        "f2_calendar_loaded": calendar_ok,
        "freeze": hashes or {},
        "books": packed,
        "equity_vs_benchmark": {k: packed[k]["equity"] for k in BOOK_ORDER},
        "open_positions": {k: packed[k]["positions"] for k in BOOK_ORDER},
        "maxDD_so_far": {k: packed[k]["maxDD"] for k in BOOK_ORDER},
        "notes": notes,
        "disclaimer": (
            "Paper / dry-run only. Not live. C_paper=10000 is configured notional, not the OKX "
            "main-account (6,788 USDT wedding funds excluded). Cypheous signed off on H5, H1 and H4 "
            "as v3 FORWARD PAPER slots; this is not a strategy PASS. Official PASS is evaluated at "
            "T+90 and T+180 only. Bars before T0 are not counted."
        ),
    }


def _write_status(art: Path, payload: dict) -> None:
    art.mkdir(parents=True, exist_ok=True)
    (art / "paper_status.json").write_text(json.dumps(payload, indent=2, default=str))
    with (art / "paper_status.jsonl").open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")

    def fmt(x):
        if x is None:
            return "—"
        if isinstance(x, float):
            return f"{x:.4f}"
        return str(x)

    def fmt_dd(x):
        return "—" if x is None else f"{100 * x:.2f}%"

    evb = payload["equity_vs_benchmark"]
    pos = payload["open_positions"]
    dd = payload["maxDD_so_far"]
    init_label = "initialized" if payload.get("t0_initialized") else "waiting"
    lines = [
        f"# Spec v3 paper status — {payload['label']}",
        "",
        f"- run_id: `{payload['run_id']}`",
        f"- official: {payload['official']}",
        f"- as of: {payload['as_of']}",
        f"- last closed 1Dutc (bar open date): {payload['last_closed_1dutc_open'] or '—'}",
        f"- waiting for T0: {payload['waiting_for_t0']}",
        f"- T0 initialized or waiting: {init_label}",
        f"- T0: {payload['t0']}",
        f"- regime state: **{payload['regime_state']}**",
        f"- F2 calendar loaded: {payload['f2_calendar_loaded']}",
        f"- errors: {payload['n_errors']}" + (f" {payload['errors']}" if payload["errors"] else ""),
        "",
        "## Freeze hashes verified",
        "",
    ]
    fr = payload.get("freeze") or {}
    if fr:
        lines += [
            f"- sha256_brief: `{fr.get('sha256_brief', '—')}`",
            f"- sha256_v3_code: `{fr.get('sha256_v3_code', '—')}`",
            f"- H1 params: `{((fr.get('params') or {}).get('H1')) or '—'}`",
            f"- H4 params: `{((fr.get('params') or {}).get('H4')) or '—'}`",
            f"- H5 params: `{((fr.get('params') or {}).get('H5')) or '—'}`",
            f"- forward_slots: {fr.get('forward_slots', '—')}",
            "",
        ]
    else:
        lines += ["- (not verified this iteration)", ""]
    lines += [
        "## Equity vs benchmark (C_paper units)",
        "",
    ]
    for k in BOOK_ORDER:
        lines.append(f"- {k}: {fmt(evb.get(k))}")
    lines += ["", "## Open positions", ""]
    for k in BOOK_ORDER:
        lines.append(f"- {k}: {pos.get(k)}")
    lines += ["", "## maxDD so far", ""]
    for k in BOOK_ORDER:
        lines.append(f"- {k}: {fmt_dd(dd.get(k))}")
    lines += ["", "## Notes", ""]
    for n in payload.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    lines.append(payload["disclaimer"])
    lines.append("")
    md = "\n".join(lines)
    (art / "paper_status.md").write_text(md)
    if payload.get("official"):
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        (ARTIFACT_ROOT / "paper_status.md").write_text(md)
        (ARTIFACT_ROOT / "paper_status.json").write_text((art / "paper_status.json").read_text())


def _load_ledger(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, default=str))


def _books_from_ledger(ledger: dict | None, capital: float, symbols: list[str]) -> dict[str, BookState]:
    if ledger and "books" in ledger:
        return {k: BookState.from_json(v) for k, v in ledger["books"].items()}
    return {
        "H5": new_book("H5", capital, symbols),
        "H1": new_book("H1", capital, symbols),
        "H4": new_book("H4", capital, symbols),
        "V-C": new_book("V-C", capital, symbols),
        "BENCH-50-50": new_book("BENCH-50-50", capital, list(P.CORE_UNIVERSE)),
    }


def _refresh_v3_cache(cfg: Config, bases: list[str]) -> None:
    from tradingbot.cli import _feed

    feed = _feed(cfg)
    feed.cache_dir = V3_CACHE
    V3_CACHE.mkdir(parents=True, exist_ok=True)
    for base in bases:
        feed.get_candles(inst_id(base), "1Dutc", days=400, update=True, closed_only=True)


def _pit_universe(panel: dict) -> pd.DataFrame:
    # Favorites membership used by H2 (not slotted). H1/H4/H5 use the panel columns.
    try:
        from tradingbot.strategies.spec_v1_signals import universe_and_breadth

        U, _ = universe_and_breadth(panel)
        return U
    except Exception:
        return pd.DataFrame()


def run_v3_iteration(
    cfg: Config,
    *,
    run_id: str = OFFICIAL_RUN_ID,
    official: bool = True,
    update: bool = True,
    panel: dict | None = None,
    regime: pd.DataFrame | None = None,
    funding_fetch=None,
    verify: bool = True,
) -> tuple[int, dict]:
    errors: list[str] = []
    notes: list[str] = []
    events: list[dict] = []
    dirs = run_dirs(run_id)
    official = official or (run_id == OFFICIAL_RUN_ID)
    hashes: dict | None = None

    if verify:
        try:
            hashes = verify_freeze()
            notes.append("Freeze hashes verified against /workspace/sprint/artifacts/spec_v3/freeze.json.")
            notes.append(f"sha256_v3_code={hashes['sha256_v3_code']}")
        except FreezeMismatch as e:
            errors.append(str(e))
            payload = _section7(
                run_id=run_id,
                official=official,
                waiting=True,
                initialized=False,
                last_closed=None,
                regime="UNKNOWN",
                books={},
                errors=errors,
                calendar_ok=False,
                notes=notes,
                hashes=None,
            )
            _write_status(dirs["art"], payload)
            return 1, {
                "message": f"REFUSED: freeze mismatch: {e}",
                "errors": errors,
                "n_errors": len(errors),
                "run_id": run_id,
                "waiting_for_t0": True,
                "equity": None,
            }

    try:
        capital = c_paper_official(cfg)
    except ValueError as e:
        errors.append(str(e))
        payload = _section7(
            run_id=run_id, official=official, waiting=True, initialized=False,
            last_closed=None, regime="UNKNOWN", books={}, errors=errors,
            calendar_ok=False, notes=notes, hashes=hashes,
        )
        _write_status(dirs["art"], payload)
        return 1, {"message": str(e), "errors": errors, "n_errors": 1, "run_id": run_id, "waiting_for_t0": True, "equity": None}

    calendar_ok = False
    try:
        ev = load_events()
        stale = calendar_is_stale()
        last = last_fetch_timestamp()
        calendar_ok = (not stale) and len(ev) > 0
        if stale:
            errors.append("F2 calendar fetch stamp is older than 7 days (fail-safe HALT would apply).")
        notes.append(f"F2 events loaded: n={len(ev)}; last fetch {last.isoformat()}.")
        if ev["tau"].max() < pd.Timestamp("2026-11-13", tz="UTC"):
            errors.append("F2 calendar does not cover the T0+24h window.")
        else:
            notes.append("F2 calendar covers the official T0 and T0+24h window.")
    except Exception as e:
        errors.append(f"F2 calendar failed: {e}")

    if panel is None:
        try:
            if update:
                _refresh_v3_cache(cfg, ["BTC", "ETH"])
            panel = v3_load_panel(cache_dir=V3_CACHE)
        except Exception as e:
            errors.append(f"data load/REST failed: {e}")
            payload = _section7(
                run_id=run_id, official=official, waiting=True, initialized=False,
                last_closed=None, regime="UNKNOWN", books={}, errors=errors,
                calendar_ok=calendar_ok, notes=notes, hashes=hashes,
            )
            _write_status(dirs["art"], payload)
            return 1, {"message": "data failure", "errors": errors, "n_errors": len(errors), "run_id": run_id, "waiting_for_t0": True, "equity": None}

    last = _last_closed_open(panel)
    last_s = str(last.date()) if last is not None else None
    waiting = last is None or last < T0_BAR

    if regime is None:
        try:
            regime = build_regime(panel)
        except Exception as e:
            errors.append(f"regime failed: {e}")
            regime = None
    if regime is not None and last is not None and last in regime.index:
        regime_state = str(regime.loc[last, "macroy_state"]) if "macroy_state" in regime.columns else "UNKNOWN"
    else:
        regime_state = "UNKNOWN"

    # H1 funding: REST at signal time only. Never archive, never backfill.
    funding_events: list[dict] = []
    crowd = pd.DataFrame()
    try:
        if funding_fetch is not None:
            fetched = funding_fetch(index=panel["close"].index, last=last)
            if isinstance(fetched, tuple) and len(fetched) == 2 and isinstance(fetched[0], dict) and not any(
                isinstance(v, pd.Series) for v in fetched[0].values()
            ) and fetched[0] == {}:
                # empty series + events (tests / hard miss)
                funding_series, funding_events = fetched
                crowd, extra = crowd_from_rest_or_off(funding_series, panel["close"].index, reason="rest_empty")
                funding_events = list(funding_events) + extra
            elif isinstance(fetched, tuple):
                funding_series, funding_events = fetched
                if funding_series and any(isinstance(v, pd.Series) for v in funding_series.values()):
                    crowd, extra = crowd_from_rest_or_off(funding_series, panel["close"].index)
                    funding_events = list(funding_events) + extra
                else:
                    crowd, extra = crowd_from_rest_or_off(funding_series or {}, panel["close"].index, reason="rest_empty")
                    funding_events = list(funding_events) + extra
            else:
                funding_series, funding_events = fetched, []
                crowd, extra = crowd_from_rest_or_off(funding_series, panel["close"].index)
                funding_events = extra
        else:
            funding_series, fetch_events = fetch_rest_funding_series(dest_dir=dirs["state"])
            crowd, extra = crowd_from_rest_or_off(funding_series, panel["close"].index)
            funding_events = fetch_events + extra
    except Exception as e:
        funding_events.append(
            {"type": "h1_funding_missing", "action": "filter_OFF", "reason": f"REST funding failed: {e}"}
        )
        crowd = pd.DataFrame()
    events.extend(funding_events)
    n_miss = sum(1 for e in funding_events if e.get("type") == "h1_funding_missing")
    if n_miss:
        notes.append(f"H1 funding: {n_miss} missing/skip event(s); filter OFF for those days. REST only; no archive backfill.")
    else:
        notes.append("H1 funding: public REST funding-rate-history only (no archive, no backfill).")

    ledger = _load_ledger(dirs["ledger"])
    symbols = list(panel["close"].columns)
    books = _books_from_ledger(ledger, capital, symbols)
    initialized = bool(ledger and ledger.get("initialized"))

    if waiting:
        notes.append(
            f"Last closed 1Dutc bar open {last_s} is before T0 bar 2026-11-11; "
            "no official fills and no counted returns yet. Waiting for the T0 close "
            "(2026-11-12 07:00 UTC+7)."
        )
        notes.append(
            "Cypheous signed off on H5, H1 and H4 as v3 FORWARD PAPER slots; this is not a strategy PASS."
        )
        payload = _section7(
            run_id=run_id, official=official, waiting=True, initialized=False,
            last_closed=last_s, regime=regime_state, books=books if initialized else {},
            errors=errors, calendar_ok=calendar_ok, notes=notes, hashes=hashes,
        )
        # Always expose the five book keys in the status, even while waiting.
        if not initialized:
            for name in BOOK_ORDER:
                payload["books"].setdefault(name, _empty_book_pack())
                payload["equity_vs_benchmark"][name] = payload["books"][name]["equity"]
                payload["open_positions"][name] = payload["books"][name]["positions"]
                payload["maxDD_so_far"][name] = payload["books"][name]["maxDD"]
        _write_status(dirs["art"], payload)
        _append_events(dirs["events"], events)
        if ledger is None:
            _save_ledger(
                dirs["ledger"],
                {
                    "run_id": run_id,
                    "official": official,
                    "initialized": False,
                    "c_paper": capital,
                    "waiting_for_t0": True,
                    "freeze": hashes,
                },
            )
        msg = (
            f"paper v3 {run_id}: waiting for T0 (last closed open={last_s}) "
            f"errors={len(errors)} official={official}"
        )
        return (0 if not errors else 1), {
            "message": msg,
            "strategy": "v3",
            "equity": capital,
            "errors": errors,
            "n_errors": len(errors),
            "run_id": run_id,
            "waiting_for_t0": True,
            "t0_initialized": False,
            "state_path": str(dirs["ledger"]),
            "hashes": hashes,
        }

    # T0 bar is in the panel (or later). Refresh the V-C / favorites universe into the v3 cache only.
    try:
        if update and official and panel is not None:
            need_full = (not initialized) or (ledger and ledger.get("last_closed") != last_s)
            if need_full:
                favs = list(load_favorites())
                _refresh_v3_cache(cfg, ["BTC", "ETH"] + favs)
                panel = v3_load_panel(cache_dir=V3_CACHE)
                regime = build_regime(panel)
                symbols = list(panel["close"].columns)
    except Exception as e:
        errors.append(f"universe refresh failed: {e}")

    if regime is None:
        errors.append("regime missing; cannot arm or step official books.")
        payload = _section7(
            run_id=run_id, official=official, waiting=False, initialized=initialized,
            last_closed=last_s, regime="UNKNOWN", books=books if initialized else {},
            errors=errors, calendar_ok=calendar_ok, notes=notes, hashes=hashes,
        )
        _write_status(dirs["art"], payload)
        _append_events(dirs["events"], events)
        return 1, {"message": "regime missing", "errors": errors, "n_errors": len(errors), "run_id": run_id, "waiting_for_t0": False, "equity": capital}

    U = _pit_universe(panel)
    hyps = build_hypotheses(panel, regime=regime, crowd=crowd, pit_universe=U)
    pres: dict[str, dict] = {}
    try:
        for hid in ("H5", "H1", "H4"):
            pres[hid] = hyps[hid].precompute(panel)
    except Exception as e:
        errors.append(f"signal precompute failed: {e}")

    if not initialized and not errors:
        books = _books_from_ledger(None, capital, symbols)
        t0 = T0_BAR
        if t0 not in panel["close"].index:
            errors.append(f"T0 bar {t0.date()} missing from closed panel; cannot arm official books.")
        else:
            try:
                row = regime.loc[t0] if t0 in regime.index else pd.Series({"macroy_state": "NORMAL"})
                for hid in ("H5", "H1", "H4"):
                    h = hyps[hid]
                    pre = pres[hid]
                    cols = list(h.symbols) if h.symbols else symbols
                    cur = pd.Series(0.0, index=cols)
                    w = h.target_fn(t0, panel, pre, cur, str(row.get("macroy_state", "NORMAL")))
                    books[hid].pending = {s: float(w.get(s, 0.0)) for s in (w.index if hasattr(w, "index") else cols)}
                    books[hid].pending_signal_date = str(t0.date())
                    books[hid].last_processed = str(t0.date())
                # Frozen V-C (v1 engine, sticky kill) — comparator only.
                pre_v1 = _prepare_v1(panel, regime)
                _step_zara(books["V-C"], t0, panel, pre_v1, regime, cost=COST, count_bar=True, arm_only=True)
                fill_date = t0 + pd.Timedelta(days=1)
                o = panel["open"]
                open_px = {}
                for s in symbols:
                    if s in o.columns and fill_date in o.index and pd.notna(o.at[fill_date, s]):
                        open_px[s] = float(o.at[fill_date, s])
                    elif s in panel["close"].columns and t0 in panel["close"].index and pd.notna(panel["close"].at[t0, s]):
                        open_px[s] = float(panel["close"].at[t0, s])
                from tradingbot.eval.v2_engine import _fill_to_weights

                for name in ("H5", "H1", "H4", "V-C"):
                    st = books[name]
                    _fill_to_weights(st, dict(st.pending), open_px, fill_date, COST, block_new=False, flatten=False)
                    mark_px = {s: open_px[s] for s in st.qty if s in open_px}
                    st.equity = st.cash + sum(float(st.qty.get(s, 0.0)) * mark_px[s] for s in mark_px)
                    if st.equity > st.peak:
                        st.peak = st.equity
                # Benchmark first entry at the same fill open. Do not count a daily return
                # until the next 1Dutc bar has actually closed.
                if fill_date in panel["close"].index:
                    step_bench(books["BENCH-50-50"], fill_date, panel, cost=COST, count_bar=True, is_first_counted=True)
                else:
                    # Arm 50/50 at the proxy open for status; first counted return later.
                    step_bench(books["BENCH-50-50"], t0, panel, cost=COST, count_bar=False, is_first_counted=False)
                    from tradingbot.eval.v2_engine import _fill_to_weights as _fill

                    _fill(books["BENCH-50-50"], {s: 0.5 for s in P.CORE_UNIVERSE}, open_px, fill_date, COST, block_new=False, flatten=False)
                    mark_px = {s: open_px[s] for s in books["BENCH-50-50"].qty if s in open_px}
                    books["BENCH-50-50"].equity = books["BENCH-50-50"].cash + sum(
                        float(books["BENCH-50-50"].qty.get(s, 0.0)) * mark_px[s] for s in mark_px
                    )
                    if books["BENCH-50-50"].equity > books["BENCH-50-50"].peak:
                        books["BENCH-50-50"].peak = books["BENCH-50-50"].equity
                    books["BENCH-50-50"].pending = {s: 0.5 for s in P.CORE_UNIVERSE}
                    books["BENCH-50-50"].last_processed = str(t0.date())
                notes.append(
                    "Official books initialised at C_paper on the T0 close; first fills at the "
                    "next 1Dutc open. Pre-T0 bars are not counted. First counted daily return "
                    "will be booked when the next 1Dutc bar closes."
                )
                initialized = True
            except Exception as e:
                errors.append(f"T0 init/fill failed: {e}")

    elif initialized and not errors:
        # Step any newly closed bars after last_processed. Fill date == bar open. Counted.
        try:
            pre_v1 = None
            last_proc = None
            for st in books.values():
                if st.last_processed:
                    ts = pd.Timestamp(st.last_processed)
                    last_proc = ts if last_proc is None else max(last_proc, ts)
            # Dates strictly after last_processed, at or after fill day (T0+1).
            start_fill = T0_BAR + pd.Timedelta(days=1)
            dates = panel["close"].index[(panel["close"].index >= start_fill)]
            if last_proc is not None:
                dates = dates[dates > last_proc]
            for dt in dates:
                row = regime.loc[dt] if dt in regime.index else pd.Series({"macroy_state": "NORMAL"})
                for hid in ("H5", "H1", "H4"):
                    h = hyps[hid]
                    from tradingbot.eval.v3_engine import step_generic

                    cols = list(h.symbols) if h.symbols else symbols
                    step_generic(
                        books[hid],
                        dt,
                        panel,
                        pres[hid],
                        row,
                        h.target_fn,
                        cols,
                        cost=COST,
                        use_band=h.use_band,
                        band_membership_only=h.band_membership_only,
                    )
                if pre_v1 is None:
                    pre_v1 = _prepare_v1(panel, regime)
                _step_zara(books["V-C"], dt, panel, pre_v1, regime, cost=COST, count_bar=True, arm_only=False)
                step_bench(books["BENCH-50-50"], dt, panel, cost=COST, count_bar=True, is_first_counted=False)
            if len(dates):
                notes.append(f"Stepped {len(dates)} new closed 1Dutc bar(s) through {last_s}.")
        except Exception as e:
            errors.append(f"step failed: {e}")

    _save_ledger(
        dirs["ledger"],
        {
            "run_id": run_id,
            "official": official,
            "initialized": initialized,
            "c_paper": capital,
            "waiting_for_t0": False,
            "last_closed": last_s,
            "t0_bar": str(T0_BAR.date()),
            "freeze": hashes,
            "books": {k: v.to_json() for k, v in books.items()},
        },
    )
    notes.append(
        "Cypheous signed off on H5, H1 and H4 as v3 FORWARD PAPER slots; this is not a strategy PASS."
    )
    payload = _section7(
        run_id=run_id, official=official, waiting=False, initialized=initialized,
        last_closed=last_s, regime=regime_state, books=books,
        errors=errors, calendar_ok=calendar_ok, notes=notes, hashes=hashes,
    )
    _write_status(dirs["art"], payload)
    _append_events(dirs["events"], events)
    eq = books["H5"].equity if initialized else capital
    msg = (
        f"paper v3 {run_id}: last closed open={last_s} initialized={initialized} "
        f"errors={len(errors)} official={official}"
    )
    return (0 if not errors else 1), {
        "message": msg,
        "strategy": "v3",
        "equity": eq,
        "errors": errors,
        "n_errors": len(errors),
        "run_id": run_id,
        "waiting_for_t0": False,
        "t0_initialized": initialized,
        "state_path": str(dirs["ledger"]),
        "hashes": hashes,
    }
