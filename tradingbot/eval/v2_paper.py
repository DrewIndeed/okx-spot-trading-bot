"""Official (and optional smoke) paper-forward loop body for spec v2.

Three books, equal C_paper: V2-CORE, V2-ZARA-FROZEN (V-C unchanged), 50/50
BTC/ETH monthly-rebalanced benchmark. Signals only on confirm==1 1Dutc bars.
First counted bar for the official run is T0 (2026-11-12 07:00 UTC+7).
Paper / dry-run only. Never live. Never reads an OKX account balance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradingbot.config import Config
from tradingbot.eval.events import calendar_is_stale, last_fetch_timestamp, load_events
from tradingbot.eval.load import load_panel
from tradingbot.eval.regime import build_regime
from tradingbot.eval.v2_engine import (
    BookState,
    new_book,
    step_bench,
    step_core,
)
from tradingbot.strategies import spec_v2_common as P
from tradingbot.strategies.spec_v2_core import core_signals, core_target_weights
from tradingbot.universe import sma

SAIGON = ZoneInfo("Asia/Saigon")
T0_BAR = pd.Timestamp(P.T0_BAR_OPEN_UTC.replace("Z", ""))  # 2026-11-11
ARTIFACT_ROOT = Path("/workspace/sprint/artifacts/spec_v2")


def run_dirs(run_id: str) -> dict[str, Path]:
    state = Path("state") / "spec_v2" / run_id
    logs = Path("logs") / "spec_v2" / run_id
    art = ARTIFACT_ROOT / run_id
    for p in (state, logs, art):
        p.mkdir(parents=True, exist_ok=True)
    return {"state": state, "logs": logs, "art": art, "ledger": state / "ledger.json"}


def c_paper_from(cfg: Config) -> float:
    c = getattr(cfg, "c_paper", None)
    if c is None:
        raise ValueError("C_paper is required for spec v2 paper; never inferred from an OKX account.")
    c = float(c)
    if c <= 0:
        raise ValueError("C_paper must be positive.")
    if abs(c - P.WEDDING_FUNDS_USDT) < 1e-9:
        raise ValueError("C_paper equals the excluded 6,788 USDT wedding funds; refuse.")
    return c


def _last_closed_open(panel: dict) -> pd.Timestamp | None:
    idx = panel["close"].index
    return None if len(idx) == 0 else pd.Timestamp(idx[-1])


def _section7(
    *,
    run_id: str,
    official: bool,
    waiting: bool,
    last_closed: str | None,
    regime: str,
    books: dict[str, BookState],
    errors: list[str],
    calendar_ok: bool,
    notes: list[str],
) -> dict:
    def pack(st: BookState | None) -> dict:
        if st is None:
            return {"equity": None, "positions": {}, "maxDD": None, "n_closed": 0, "killed": None}
        eq = st.equity
        peak = st.peak or eq
        dd = (eq / peak - 1.0) if peak else 0.0
        # maxDD of counted equity curve if present
        if st.equity_curve:
            ser = pd.Series([r["equity"] for r in st.equity_curve])
            dd = float((ser / ser.cummax() - 1.0).min()) if len(ser) else dd
        pos = {s: q for s, q in st.qty.items() if abs(float(q)) > 1e-12}
        return {
            "equity": st.equity,
            "cash": st.cash,
            "positions": pos,
            "maxDD": dd,
            "n_closed": len(st.trades_closed),
            "n_open": len(st.trades_open),
            "killed": st.killed,
            "killed_on": st.killed_on,
            "last_processed": st.last_processed,
        }

    core = pack(books.get("V2-CORE"))
    zara = pack(books.get("V2-ZARA-FROZEN"))
    bench = pack(books.get("BENCH-50-50"))
    return {
        "run_id": run_id,
        "official": official,
        "label": "OFFICIAL spec v2 paper-forward" if official else "SMOKE — unofficial until Andrew approves v2",
        "waiting_for_t0": waiting,
        "t0": "2026-11-12 07:00 UTC+7 (1Dutc close)",
        "last_closed_1dutc_open": last_closed,
        "as_of": datetime.now(SAIGON).strftime("%Y-%m-%d %H:%M:%S %z") + " (Asia/Saigon, UTC+7)",
        "regime_state": regime,
        "errors": errors,
        "n_errors": len(errors),
        "f2_calendar_loaded": calendar_ok,
        "books": {"V2-CORE": core, "V2-ZARA-FROZEN": zara, "BENCH-50-50": bench},
        "equity_vs_benchmark": {
            "V2-CORE": core["equity"],
            "V2-ZARA-FROZEN": zara["equity"],
            "BENCH-50-50": bench["equity"],
        },
        "open_positions": {
            "V2-CORE": core["positions"],
            "V2-ZARA-FROZEN": zara["positions"],
            "BENCH-50-50": bench["positions"],
        },
        "maxDD_so_far": {
            "V2-CORE": core["maxDD"],
            "V2-ZARA-FROZEN": zara["maxDD"],
            "BENCH-50-50": bench["maxDD"],
        },
        "notes": notes,
        "disclaimer": "Paper / dry-run only. Not live. C_paper is configured notional, not the OKX main-account (wedding funds excluded). Official PASS is evaluated at T+90 and T+180 only.",
    }


def _write_status(art: Path, payload: dict) -> None:
    (art / "paper_status.json").write_text(json.dumps(payload, indent=2, default=str))
    with (art / "paper_status.jsonl").open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")
    evb = payload["equity_vs_benchmark"]
    pos = payload["open_positions"]
    dd = payload["maxDD_so_far"]

    def fmt(x):
        if x is None:
            return "—"
        if isinstance(x, float):
            return f"{x:.4f}"
        return str(x)

    def fmt_dd(x):
        return "—" if x is None else f"{100*x:.2f}%"

    lines = [
        f"# Spec v2 paper status — {payload['label']}",
        "",
        f"- run_id: `{payload['run_id']}`",
        f"- official: {payload['official']}",
        f"- as of: {payload['as_of']}",
        f"- last closed 1Dutc (bar open date): {payload['last_closed_1dutc_open'] or '—'}",
        f"- waiting for T0: {payload['waiting_for_t0']}",
        f"- T0: {payload['t0']}",
        f"- regime state: **{payload['regime_state']}**",
        f"- F2 calendar loaded: {payload['f2_calendar_loaded']}",
        f"- errors: {payload['n_errors']}" + (f" {payload['errors']}" if payload['errors'] else ""),
        "",
        "## Equity vs benchmark (C_paper units)",
        "",
        f"- V2-CORE: {fmt(evb['V2-CORE'])}",
        f"- V2-ZARA-FROZEN: {fmt(evb['V2-ZARA-FROZEN'])}",
        f"- BENCH-50-50: {fmt(evb['BENCH-50-50'])}",
        "",
        "## Open positions",
        "",
        f"- V2-CORE: {pos['V2-CORE']}",
        f"- V2-ZARA-FROZEN: {pos['V2-ZARA-FROZEN']}",
        f"- BENCH-50-50: {pos['BENCH-50-50']}",
        "",
        "## maxDD so far",
        "",
        f"- V2-CORE: {fmt_dd(dd['V2-CORE'])}",
        f"- V2-ZARA-FROZEN: {fmt_dd(dd['V2-ZARA-FROZEN'])}",
        f"- BENCH-50-50: {fmt_dd(dd['BENCH-50-50'])}",
        "",
        "## Notes",
        "",
    ]
    for n in payload.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    lines.append(payload["disclaimer"])
    lines.append("")
    (art / "paper_status.md").write_text("\n".join(lines))
    # convenience copies at the spec_v2 root for the official run
    if payload.get("official"):
        (ARTIFACT_ROOT / "paper_status.md").write_text((art / "paper_status.md").read_text())
        (ARTIFACT_ROOT / "paper_status.json").write_text((art / "paper_status.json").read_text())


def _load_ledger(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, default=str))


def _books_from_ledger(ledger: dict | None, capital: float) -> dict[str, BookState]:
    if not ledger or "books" not in ledger:
        return {
            "V2-CORE": new_book("V2-CORE", capital, list(P.CORE_UNIVERSE)),
            "V2-ZARA-FROZEN": new_book("V2-ZARA-FROZEN", capital, []),  # filled when panel known
            "BENCH-50-50": new_book("BENCH-50-50", capital, list(P.CORE_UNIVERSE)),
        }
    out = {}
    for k, v in ledger["books"].items():
        out[k] = BookState.from_json(v)
    return out


# --- V-C (ZARA) one-bar step via v1 primitives (imported, not modified) ---

def _prepare_v1(panel: dict, regime: pd.DataFrame) -> dict:
    from tradingbot.strategies.spec_v1_signals import (
        universe_and_breadth,
        va_indicators,
        vb_signals,
        vc_signals,
        vol_ann,
    )
    import numpy as np

    U, br = universe_and_breadth(panel)
    return {
        "U": U,
        "breadth": br,
        "vol": vol_ann(panel["close"]),
        "log_rets": np.log(panel["close"]).diff(),
        "va": va_indicators(panel, U, br, regime["r1_on"]),
        "vb": vb_signals(panel, U),
        "vc": vc_signals(panel, U),
    }


def _step_zara(
    st: BookState,
    date: pd.Timestamp,
    panel: dict,
    pre_v1: dict,
    regime: pd.DataFrame,
    *,
    cost: float,
    count_bar: bool,
    arm_only: bool = False,
) -> None:
    """V-C targets via compute_desired; fills with the shared BookState helper.

    va_book is unused for V-C. State of weights is in st.qty.
    If arm_only, compute pending from this close and do not fill (used at T0).
    """
    from tradingbot.eval.engine import compute_desired
    from tradingbot.eval.v2_engine import _fill_to_weights, _mark

    if not count_bar:
        st.last_processed = str(pd.Timestamp(date).date())
        return

    opens, close = panel["open"], panel["close"]
    cols = list(close.columns)
    if not st.qty:
        st.qty = {s: 0.0 for s in cols}

    o = {s: (float(opens.at[date, s]) if s in opens.columns and pd.notna(opens.at[date, s]) else np.nan) for s in st.qty}
    c = {s: (float(close.at[date, s]) if s in close.columns and pd.notna(close.at[date, s]) else np.nan) for s in st.qty}

    if arm_only:
        cur = pd.Series(0.0, index=cols)
        va_book: dict = {}
        desired, _, _ = compute_desired("V-C", date, panel, pre_v1, regime, cur, va_book, f2_for_va=True)
        st.pending = {s: float(desired.get(s, 0.0)) for s in desired.index if float(desired.get(s, 0.0)) > 0 or s in st.qty}
        st.pending_signal_date = str(pd.Timestamp(date).date())
        st.last_processed = str(pd.Timestamp(date).date())
        return

    flatten = bool(st.kill_pending or (st.killed and sum(abs(float(q)) for q in st.qty.values()) > 1e-12))
    if st.kill_pending:
        st.killed = True
        if st.killed_on is None:
            st.killed_on = str(pd.Timestamp(date).date())
        st.kill_pending = False
    block = st.block_new
    st.block_new = False
    traded = _fill_to_weights(st, dict(st.pending), o, date, cost, block_new=block, flatten=flatten)
    r = _mark(st, c, date, traded)
    if st.equity > st.peak:
        st.peak = st.equity
    dd = (st.equity / st.peak - 1.0) if st.peak else 0.0
    # V-C kill-switch is STICKY (spec v1 unchanged)
    if (not st.killed) and dd <= -0.15:
        st.kill_pending = True
    if r <= -0.02:
        st.block_new = True

    if st.killed or st.kill_pending:
        st.pending = {s: 0.0 for s in st.qty}
    else:
        cur = pd.Series(
            {s: (float(st.qty.get(s, 0.0)) * float(c[s]) / st.equity) if st.equity and pd.notna(c.get(s)) else 0.0
             for s in cols}
        )
        va_book = {}
        desired, _, _ = compute_desired("V-C", date, panel, pre_v1, regime, cur, va_book, f2_for_va=True)
        st.pending = {s: float(desired.get(s, 0.0)) for s in desired.index}
        st.pending_signal_date = str(pd.Timestamp(date).date())
    st.last_processed = str(pd.Timestamp(date).date())


def run_v2_iteration(
    cfg: Config,
    *,
    run_id: str = P.OFFICIAL_RUN_ID,
    official: bool = True,
    update: bool = True,
) -> tuple[int, dict]:
    errors: list[str] = []
    notes: list[str] = []
    dirs = run_dirs(run_id)
    capital = c_paper_from(cfg)
    official = official or (run_id == P.OFFICIAL_RUN_ID)

    # F2 calendar check (do not modify the calendar files; they are in the v1 hash set)
    calendar_ok = False
    try:
        ev = load_events()
        stale = calendar_is_stale()
        last = last_fetch_timestamp()
        calendar_ok = (not stale) and len(ev) > 0
        if stale:
            errors.append("F2 calendar fetch stamp is older than 7 days (fail-safe HALT would apply).")
        notes.append(f"F2 events loaded: n={len(ev)}; last fetch {last.isoformat()}.")
        # coverage of T0 / T0+24h
        if ev["tau"].max() < pd.Timestamp("2026-11-13", tz="UTC"):
            errors.append("F2 calendar does not cover the T0+24h window.")
        else:
            notes.append("F2 calendar covers the official T0 and T0+24h window.")
    except Exception as e:
        errors.append(f"F2 calendar failed: {e}")

    # Load closed 1Dutc only (confirm==1 in load_panel)
    try:
        if update:
            # Public REST refresh of BTC (and ETH) 1Dutc; full universe refresh only when needed
            from tradingbot.cli import _feed
            feed = _feed(cfg)
            for inst in ("BTC-USDT", "ETH-USDT"):
                feed.get_candles(inst, "1Dutc", days=400, update=True, closed_only=True)
        panel = load_panel()
    except Exception as e:
        errors.append(f"data load/REST failed: {e}")
        payload = _section7(
            run_id=run_id, official=official, waiting=True, last_closed=None,
            regime="UNKNOWN", books={}, errors=errors, calendar_ok=calendar_ok, notes=notes,
        )
        _write_status(dirs["art"], payload)
        return 1, {"message": "data failure", "errors": errors, "equity": None, "run_id": run_id}

    last = _last_closed_open(panel)
    last_s = str(last.date()) if last is not None else None
    waiting = last is None or last < T0_BAR

    # Regime on last closed bar (for status); only closed data in panel
    try:
        regime = build_regime(panel)
        if last is not None and last in regime.index:
            regime_state = str(regime.loc[last, "macroy_state"])
        else:
            regime_state = "UNKNOWN"
    except Exception as e:
        errors.append(f"regime failed: {e}")
        regime = None
        regime_state = "UNKNOWN"

    ledger = _load_ledger(dirs["ledger"])
    books = _books_from_ledger(ledger, capital)
    initialized = bool(ledger and ledger.get("initialized"))

    if waiting:
        notes.append(
            f"Last closed 1Dutc bar open {last_s} is before T0 bar 2026-11-11; "
            "no official fills and no counted returns yet. Waiting for the T0 close "
            "(2026-11-12 07:00 UTC+7)."
        )
        payload = _section7(
            run_id=run_id, official=official, waiting=True, last_closed=last_s,
            regime=regime_state, books=books if initialized else {},
            errors=errors, calendar_ok=calendar_ok, notes=notes,
        )
        _write_status(dirs["art"], payload)
        if ledger is None:
            _save_ledger(dirs["ledger"], {
                "run_id": run_id,
                "official": official,
                "initialized": False,
                "c_paper": capital,
                "waiting_for_t0": True,
            })
        msg = (
            f"paper v2 {run_id}: waiting for T0 (last closed open={last_s}) "
            f"errors={len(errors)} official={official}"
        )
        return (0 if not errors else 1), {
            "message": msg, "strategy": "v2", "equity": capital, "errors": errors,
            "run_id": run_id, "waiting_for_t0": True, "n_errors": len(errors),
            "state_path": str(dirs["ledger"]),
        }

    # T0 bar is in the panel. Refresh full favorites once when initializing or on a new close.
    try:
        from tradingbot.cli import _feed
        from tradingbot.universe import load_favorites, inst_id
        if update and official:
            feed = _feed(cfg)
            # When a new counted close appears, refresh the V-C universe caches.
            need_full = (not initialized) or (ledger and ledger.get("last_closed") != last_s)
            if need_full:
                for base in load_favorites():
                    feed.get_candles(inst_id(base), "1Dutc", days=400, update=True, closed_only=True)
                panel = load_panel()
                regime = build_regime(panel)
    except Exception as e:
        errors.append(f"universe refresh failed: {e}")

    try:
        pre_core = core_signals(panel)
        pre_v1 = None  # lazy on zara step
    except Exception as e:
        errors.append(f"signal precompute failed: {e}")
        pre_core = None

    if not initialized:
        # Start flat at C_paper. First signals = T0 close. First fills = open of T0+1.
        books = {
            "V2-CORE": new_book("V2-CORE", capital, list(P.CORE_UNIVERSE)),
            "V2-ZARA-FROZEN": new_book("V2-ZARA-FROZEN", capital, list(panel["close"].columns)),
            "BENCH-50-50": new_book("BENCH-50-50", capital, list(P.CORE_UNIVERSE)),
        }
        t0 = T0_BAR
        if t0 not in panel["close"].index:
            errors.append(f"T0 bar {t0.date()} missing from closed panel; cannot arm official books.")
        elif pre_core is not None and regime is not None:
            try:
                # Arm V2-CORE pending from T0 close (no pre-T0 fill).
                row = regime.loc[t0]
                cur = pd.Series(0.0, index=list(P.CORE_UNIVERSE))
                w = core_target_weights(t0, panel, pre_core, cur, str(row["macroy_state"]))
                books["V2-CORE"].pending = {s: float(w.get(s, 0.0)) for s in w.index}
                books["V2-CORE"].pending_signal_date = str(t0.date())
                books["V2-CORE"].last_processed = str(t0.date())
                # Arm V-C from T0 close
                pre_v1 = _prepare_v1(panel, regime)
                _step_zara(books["V2-ZARA-FROZEN"], t0, panel, pre_v1, regime, cost=P.COST_2X, count_bar=True, arm_only=True)
                # Fill all three at the next open (T0 clock instant = open of 2026-11-12)
                fill_date = t0 + pd.Timedelta(days=1)
                o = panel["open"]
                # If the next bar open is missing, use T0 close as 24/7 proxy (crypto)
                open_px = {}
                for s in list(P.CORE_UNIVERSE) + list(panel["close"].columns):
                    if s in o.columns and fill_date in o.index and pd.notna(o.at[fill_date, s]):
                        open_px[s] = float(o.at[fill_date, s])
                    elif s in panel["close"].columns and t0 in panel["close"].index and pd.notna(panel["close"].at[t0, s]):
                        open_px[s] = float(panel["close"].at[t0, s])
                from tradingbot.eval.v2_engine import _fill_to_weights, _mark
                for name in ("V2-CORE", "V2-ZARA-FROZEN"):
                    st = books[name]
                    _fill_to_weights(st, dict(st.pending), open_px, fill_date, P.COST_2X, block_new=False, flatten=False)
                    # Mark at last known (T0 close / next open) for status; first counted return
                    # is recorded when the next bar closes (2026-11-13 07:00 UTC+7).
                    mark_px = {s: open_px[s] for s in st.qty if s in open_px}
                    st.equity = st.cash + sum(float(st.qty.get(s, 0.0)) * mark_px[s] for s in mark_px)
                    if st.equity > st.peak:
                        st.peak = st.equity
                # Benchmark first entry at the same fill open, 50/50, 2x fees.
                step_bench(
                    books["BENCH-50-50"], fill_date, panel,
                    cost=P.COST_2X, count_bar=True, is_first_counted=True,
                )
                # If the fill date bar is not yet closed, the bench step would have marked at
                # a missing close — handle by marking at open if close is NaN (already in _mark).
                notes.append(
                    "Official books initialised at C_paper on the T0 close; first fills at the "
                    "next 1Dutc open. Pre-T0 bars are not counted. First counted daily return "
                    "will be booked when the next 1Dutc bar closes."
                )
                initialized = True
            except Exception as e:
                errors.append(f"T0 init/fill failed: {e}")
        _save_ledger(dirs["ledger"], {
            "run_id": run_id,
            "official": official,
            "initialized": initialized,
            "c_paper": capital,
            "t0_bar": str(T0_BAR.date()),
            "last_closed": last_s,
            "books": {k: v.to_json() for k, v in books.items()},
        })
    else:
        # Process any new closed bars strictly after last_processed / after T0.
        try:
            last_proc = None
            for st in books.values():
                if st.last_processed:
                    ts = pd.Timestamp(st.last_processed)
                    last_proc = ts if last_proc is None else max(last_proc, ts)
            # After T0 init, last_processed is T0 bar date (2026-11-11). Next counted
            # close is 2026-11-12 (closes 2026-11-13 07:00).
            new_dates = panel["close"].index[(panel["close"].index > T0_BAR) & (panel["close"].index <= last)]
            if last_proc is not None:
                new_dates = new_dates[new_dates > last_proc]
            if len(new_dates):
                pre_v1 = _prepare_v1(panel, regime)
                first_bench_already = any(r.get("date") for r in books["BENCH-50-50"].equity_curve)
                for dt in new_dates:
                    step_core(books["V2-CORE"], dt, panel, pre_core, regime.loc[dt], cost=P.COST_2X, count_bar=True)
                    _step_zara(books["V2-ZARA-FROZEN"], dt, panel, pre_v1, regime, cost=P.COST_2X, count_bar=True)
                    step_bench(
                        books["BENCH-50-50"], dt, panel, cost=P.COST_2X,
                        count_bar=True, is_first_counted=not first_bench_already,
                    )
                    first_bench_already = True
                notes.append(f"Processed new counted closes: {[str(d.date()) for d in new_dates]}.")
            else:
                notes.append("No new counted close since last iteration; mark-to-market / status only.")
            _save_ledger(dirs["ledger"], {
                "run_id": run_id,
                "official": official,
                "initialized": True,
                "c_paper": capital,
                "t0_bar": str(T0_BAR.date()),
                "last_closed": last_s,
                "books": {k: v.to_json() for k, v in books.items()},
            })
        except Exception as e:
            errors.append(f"step failed: {e}")

    eq_core = books["V2-CORE"].equity if initialized else capital
    payload = _section7(
        run_id=run_id, official=official, waiting=False, last_closed=last_s,
        regime=regime_state, books=books if initialized else {},
        errors=errors, calendar_ok=calendar_ok, notes=notes,
    )
    _write_status(dirs["art"], payload)
    msg = (
        f"paper v2 {run_id}: last_closed_open={last_s} equity_core={eq_core:.2f} "
        f"errors={len(errors)} official={official} initialized={initialized}"
    )
    return (0 if not errors else 1), {
        "message": msg,
        "strategy": "v2",
        "equity": eq_core,
        "errors": errors,
        "n_errors": len(errors),
        "run_id": run_id,
        "waiting_for_t0": False,
        "initialized": initialized,
        "state_path": str(dirs["ledger"]),
        "status_md": str(dirs["art"] / "paper_status.md"),
    }
