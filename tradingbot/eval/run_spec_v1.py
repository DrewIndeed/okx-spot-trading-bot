#!/usr/bin/env python3
"""Run the frozen strategy-spec v1 walk-forward, then lock, then holdouts once.

Paper/backtest only. Capital comes from config allocated_capital. Never reads
OKX balances or wedding funds. Does not modify /workspace/crypto.
"""

from __future__ import annotations

import json
import math
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from tradingbot.config import load_config
from tradingbot.eval.engine import btc_buy_and_hold, run_path
from tradingbot.eval.load import load_panel
from tradingbot.eval.metrics import dsr_from_returns, sharpe_daily_series, window_metrics
from tradingbot.eval.protocol import (
    ARTIFACT_DIR,
    HOLDOUT,
    LOCK_PATH,
    WF_METRICS,
    HoldoutAlreadyRun,
    assert_holdout_not_run,
    combine_verdicts,
    sha256_files,
    sha256_params,
    ts_saigon,
    windows,
    write_lock,
    _verdict_fold,
    _verdict_holdout,
    _verdict_pooled,
)
from tradingbot.eval.regime import build_regime
from tradingbot.strategies import spec_v1_common as P
from tradingbot.strategies.spec_v1_signals import (
    universe_and_breadth,
    va_indicators,
    vb_signals,
    vc_signals,
    vol_ann,
)

# Guard: wedding funds and balance modules must never be imported for sizing.
FORBIDDEN_IMPORTS = ("okx_balance_readonly", "okx_onchain_readonly")


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (pd.Timestamp,)):
        return str(x)
    if isinstance(x, (np.floating, float)):
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return None if (isinstance(x, float) and math.isnan(x)) else ("inf" if x > 0 else "-inf")
        return float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    return x


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(obj), indent=2, default=str))


def _metrics_row(res, start, end, killed_on=None) -> dict:
    m = window_metrics(
        res.equity, res.rets, res.trades, start, end,
        exposure=res.exposure, turnover=res.turnover,
    )
    if killed_on is not None and pd.Timestamp(killed_on) < pd.Timestamp(start):
        m["killed_before_window"] = True
    elif killed_on is not None and pd.Timestamp(start) <= pd.Timestamp(killed_on) <= pd.Timestamp(end):
        m["killed_in_window"] = True
        m["killed_on"] = str(pd.Timestamp(killed_on).date())
    else:
        m["killed_before_window"] = False
    return m


def _precompute(panel, regime):
    print("precompute universe / signals …", flush=True)
    U, breadth = universe_and_breadth(panel)
    vol = vol_ann(panel["close"])
    log_rets = np.log(panel["close"]).diff()
    va = va_indicators(panel, U, breadth, regime["r1_on"])
    print("  V-A indicators done", flush=True)
    vb = vb_signals(panel, U)
    print("  V-B signals done", flush=True)
    vc = vc_signals(panel, U)
    print("  V-C signals done", flush=True)
    return {"U": U, "breadth": breadth, "vol": vol, "log_rets": log_rets, "va": va, "vb": vb, "vc": vc}


def _concat_results(a, b):
    """Stitch two adjacent EngineResult segments (WF + holdout). Trades already accumulated on b."""
    from tradingbot.eval.engine import EngineResult

    return EngineResult(
        equity=pd.concat([a.equity, b.equity]).sort_index(),
        rets=pd.concat([a.rets, b.rets]).sort_index(),
        exposure=pd.concat([a.exposure, b.exposure]).sort_index(),
        turnover=pd.concat([a.turnover, b.turnover]).sort_index(),
        trades=b.trades,
        killed_on=b.killed_on or a.killed_on,
        gaps=b.gaps,
        variant=b.variant,
        cost_label=b.cost_label,
        cost=b.cost,
        capital=b.capital,
        last_closed=b.last_closed,
        regime_log=pd.concat([a.regime_log, b.regime_log], ignore_index=True) if len(a.regime_log) or len(b.regime_log) else b.regime_log,
        end_state=b.end_state,
    )


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    cfg_path = Path("/workspace/trading-bot/config.yaml")
    cfg = load_config(str(cfg_path))
    capital = cfg.allocated_capital
    if capital is None or float(capital) <= 0:
        raise SystemExit("allocated_capital is required and must be positive.")
    if abs(float(capital) - P.WEDDING_FUNDS_USDT) < 1e-9:
        raise SystemExit("allocated_capital equals the excluded 6,788 USDT wedding-funds figure; refuse to run.")
    # Sanity: forbidden modules not imported
    for name in FORBIDDEN_IMPORTS:
        if name in sys.modules:
            raise SystemExit(f"{name} is loaded; it must not be wired into this evaluation.")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    panel = load_panel()
    last_closed = pd.Timestamp(panel["last_closed"])
    wins = windows(last_closed)
    print(f"last closed 1Dutc: {last_closed.date()}  capital(C)={capital}", flush=True)

    regime = build_regime(panel)
    n_halt = int(regime["f2_halt"].sum())
    print(f"F2 HALT bars: {n_halt}", flush=True)
    pre = _precompute(panel, regime)

    path_start = pd.Timestamp(P.PATH_START)
    buffer_end = wins["BUFFER"][1]
    variants = list(P.VARIANT_IDS)
    costs = list(P.COST_LEVELS)

    # --- Stage 1: continuous path through buffer end (scored OOS + descriptive buffer) ---
    results = {}  # (variant, cost) -> EngineResult (WF segment)
    states = {}
    for v in variants:
        for cl in costs:
            print(f"WF path {v} {cl} …", flush=True)
            res = run_path(v, cl, panel, pre, regime, capital, path_start, buffer_end)
            results[(v, cl)] = res
            states[(v, cl)] = res.end_state
            print(
                f"  days={len(res.rets)} n_closed={sum(1 for t in res.trades if t.exit_date is not None)} "
                f"killed_on={res.killed_on}",
                flush=True,
            )

    # Comparators at primary cost + BTC B&H (1×, pooled window)
    print("comparators …", flush=True)
    d1_no_f2 = run_path("V-A", "2x", panel, pre, regime, capital, path_start, buffer_end, disable_f2=True)
    btc_eq_p, btc_r_p = btc_buy_and_hold(panel, wins["POOLED_OOS"][0], wins["POOLED_OOS"][1], capital, P.COST_LEVELS["1x"])
    btc_sharpe_pooled = sharpe_daily_series(btc_r_p)

    # Slice WF metrics
    wf = {
        "written_at": ts_saigon(),
        "capital": float(capital),
        "capital_source": str(cfg_path) + " allocated_capital",
        "last_closed_at_wf": str(buffer_end.date()),
        "last_closed_available": str(last_closed.date()),
        "survivorship": P.SPEC_V1_PARAMS["survivorship"],
        "sha256_strategy_code": sha256_files(),
        "sha256_params": sha256_params(),
        "btc_bh_pooled_1x": {
            "Sharpe": btc_sharpe_pooled,
            "n_days": int(len(btc_r_p)),
            "start": str(wins["POOLED_OOS"][0].date()),
            "end": str(wins["POOLED_OOS"][1].date()),
        },
        "d1_no_f2_comparator_2x": {},
        "variants": {},
        "notes": [
            "Official n per window = completed round-trips with exit in that window. n_opened is also reported so a pooled total cannot hide a thin fold.",
            "Equity starts at 1.0×C, flat, on 2023-01-01; signals/indicators use expanding closed history through 2022-12-31. A live signal on 2022-12-31 is filled at the 2023-01-01 open (fresh entry, no inherited warmup position).",
            "σ uses log close-to-close returns, 90 sessions, min_periods=60, ×√365 (research4.vol_ann convention).",
            "Forecast portfolio vol uses the 90-session annualized covariance of those log returns. If the window is short, vol scaling is skipped (name/gross caps still apply).",
            "BTC buy-and-hold: one entry at the first open of the pooled window, 1× costs, no exit cost.",
            "The 6,788 USDT main-account wedding funds are excluded and were not used as C.",
        ],
    }

    # D1 no-F2 comparator slices (2×)
    wf["d1_no_f2_comparator_2x"] = {
        "POOLED_OOS": _metrics_row(d1_no_f2, *wins["POOLED_OOS"], d1_no_f2.killed_on),
        "folds": {k: _metrics_row(d1_no_f2, *wins[k], d1_no_f2.killed_on) for k in ("F1", "F2", "F3", "F4")},
        "BUFFER": _metrics_row(d1_no_f2, *wins["BUFFER"], d1_no_f2.killed_on),
    }

    pooled_rets_2x = {}
    fold_tables = []

    for v in variants:
        wf["variants"][v] = {"name": P.VARIANT_NAMES[v], "costs": {}}
        for cl in costs:
            res = results[(v, cl)]
            folds = {k: _metrics_row(res, *wins[k], res.killed_on) for k in ("F1", "F2", "F3", "F4")}
            pooled = _metrics_row(res, *wins["POOLED_OOS"], res.killed_on)
            buf = _metrics_row(res, *wins["BUFFER"], res.killed_on)
            wf["variants"][v]["costs"][cl] = {
                "folds": folds,
                "POOLED_OOS": pooled,
                "BUFFER": buf,
                "killed_on": None if res.killed_on is None else str(pd.Timestamp(res.killed_on).date()),
            }
            if cl == "2x":
                pooled_rets_2x[v] = res.rets.loc[
                    (res.rets.index >= wins["POOLED_OOS"][0]) & (res.rets.index <= wins["POOLED_OOS"][1])
                ]
            for fk, fm in folds.items():
                fold_tables.append({
                    "variant": v,
                    "name": P.VARIANT_NAMES[v],
                    "cost": cl,
                    "window": fk,
                    "start": fm["start"],
                    "end": fm["end"],
                    "n_days": fm["n_days"],
                    "n": fm["n"],
                    "n_opened": fm["n_opened"],
                    "n_closed": fm["n_closed"],
                    "Sharpe": fm["Sharpe"],
                    "maxDD": fm["maxDD"],
                    "PF": fm["PF"],
                    "expectancy_R": fm["expectancy_R"],
                    "win_rate": fm["win_rate"],
                    "CAGR": fm["CAGR"],
                    "exposure": fm["exposure"],
                    "evaluable_2x": (cl == "2x" and fm["n_days"] >= P.FOLD_MIN_DAYS and fm["n"] >= P.FOLD_MIN_TRADES),
                })

    # DSR: variance of the 3 variants' daily Sharpes on pooled OOS at 2× (research4 definition)
    daily_srs = {v: float(pooled_rets_2x[v].mean() / pooled_rets_2x[v].std()) if pooled_rets_2x[v].std() else 0.0
                 for v in variants}
    # pandas var ddof=1; if constant, 0
    sr_var = float(pd.Series(daily_srs).var(ddof=1)) if len(daily_srs) > 1 else 0.0
    dsr_block = {"sr_trials_var": sr_var, "daily_sharpes": daily_srs, "research4_PBO_context": P.RESEARCH4_PBO}
    for v in variants:
        d3 = dsr_from_returns(pooled_rets_2x[v], sr_var, P.DSR_N_TRIALS)
        d38 = dsr_from_returns(pooled_rets_2x[v], sr_var, P.DSR_N_CONSERVATIVE)
        wf["variants"][v]["DSR_2x_pooled"] = {"N3": d3, "N38": d38}
        dsr_block[v] = {"N3": d3, "N38": d38}
    wf["dsr"] = dsr_block

    # Partial §8 verdicts (folds + pooled only; holdouts pending)
    va_sharpe_2x = wf["variants"]["V-A"]["costs"]["2x"]["POOLED_OOS"]["Sharpe"]
    prelim = {}
    for v in variants:
        folds_2x = [wf["variants"][v]["costs"]["2x"]["folds"][k] for k in ("F1", "F2", "F3", "F4")]
        g81 = _verdict_fold(folds_2x)
        g82 = _verdict_pooled(
            wf["variants"][v]["costs"]["2x"]["POOLED_OOS"],
            wf["variants"][v]["costs"]["25bps"]["POOLED_OOS"],
            wf["variants"][v]["costs"]["50bps"]["POOLED_OOS"],
            btc_sharpe_pooled,
            None if v == "V-A" else va_sharpe_2x,
            v,
            wf["variants"][v]["DSR_2x_pooled"]["N3"],
        )
        prelim[v] = {"8.1": g81, "8.2": g82}
    wf["prelim_verdicts_wf_only"] = prelim

    _write(WF_METRICS, wf)
    pd.DataFrame(fold_tables).to_csv(ARTIFACT_DIR / "wf_per_fold.csv", index=False)
    # also per-variant per-cost JSON already inside wf_metrics
    print(f"wrote {WF_METRICS}", flush=True)

    lock = write_lock(wf_written=True, extra={"buffer_end": str(buffer_end.date()), "capital": float(capital)})
    print(f"wrote lock {lock}", flush=True)

    # --- Stage 2: holdouts exactly once, resuming the sticky path ---
    holdout_payload = {"H1": {}, "H2": {}}
    full_results = {}  # after H1+H2 stitch
    try:
        assert_holdout_not_run("H1")
        assert_holdout_not_run("H2")
    except HoldoutAlreadyRun as e:
        print(str(e), flush=True)
        return 2

    for v in variants:
        for cl in costs:
            print(f"H1 path {v} {cl} …", flush=True)
            h1 = run_path(
                v, cl, panel, pre, regime, capital,
                wins["H1"][0], wins["H1"][1],
                resume=states[(v, cl)],
            )
            print(f"H2 path {v} {cl} …", flush=True)
            # Continue H2 from H1 end state (one continuous path)
            h2 = run_path(
                v, cl, panel, pre, regime, capital,
                wins["H2"][0], wins["H2"][1],
                resume=h1.end_state,
            )
            full = _concat_results(_concat_results(results[(v, cl)], h1), h2)
            full_results[(v, cl)] = full
            if v not in holdout_payload["H1"]:
                holdout_payload["H1"][v] = {"name": P.VARIANT_NAMES[v], "costs": {}}
                holdout_payload["H2"][v] = {"name": P.VARIANT_NAMES[v], "costs": {}}
            holdout_payload["H1"][v]["costs"][cl] = _metrics_row(full, *wins["H1"], full.killed_on)
            holdout_payload["H2"][v]["costs"][cl] = _metrics_row(full, *wins["H2"], full.killed_on)
            holdout_payload["H1"][v]["costs"][cl]["killed_on"] = None if full.killed_on is None else str(pd.Timestamp(full.killed_on).date())
            holdout_payload["H2"][v]["costs"][cl]["killed_on"] = None if full.killed_on is None else str(pd.Timestamp(full.killed_on).date())

    # Also continue D1-no-F2 and BTC B&H for holdout reporting (comparators, not trials)
    d1_h1 = run_path("V-A", "2x", panel, pre, regime, capital, wins["H1"][0], wins["H1"][1], disable_f2=True, resume=d1_no_f2.end_state)
    d1_h2 = run_path("V-A", "2x", panel, pre, regime, capital, wins["H2"][0], wins["H2"][1], disable_f2=True, resume=d1_h1.end_state)
    d1_full = _concat_results(_concat_results(d1_no_f2, d1_h1), d1_h2)
    btc_eq_h1, btc_r_h1 = btc_buy_and_hold(panel, wins["H1"][0], wins["H1"][1], capital, P.COST_LEVELS["1x"])
    btc_eq_h2, btc_r_h2 = btc_buy_and_hold(panel, wins["H2"][0], wins["H2"][1], capital, P.COST_LEVELS["1x"])

    meta = {
        "written_at": ts_saigon(),
        "lock": str(LOCK_PATH),
        "sha256_strategy_code": sha256_files(),
        "sha256_params": sha256_params(),
        "last_closed": str(last_closed.date()),
        "survivorship": P.SPEC_V1_PARAMS["survivorship"],
        "capital": float(capital),
        "comparators": {
            "d1_no_f2_2x": {
                "H1": _metrics_row(d1_full, *wins["H1"], d1_full.killed_on),
                "H2": _metrics_row(d1_full, *wins["H2"], d1_full.killed_on),
            },
            "btc_bh_1x": {
                "H1": {"Sharpe": sharpe_daily_series(btc_r_h1), "n_days": int(len(btc_r_h1))},
                "H2": {"Sharpe": sharpe_daily_series(btc_r_h2), "n_days": int(len(btc_r_h2))},
            },
        },
    }
    h1_out = {**meta, "window": "H1", "start": str(wins["H1"][0].date()), "end": str(wins["H1"][1].date()), "variants": holdout_payload["H1"]}
    h2_out = {**meta, "window": "H2", "start": str(wins["H2"][0].date()), "end": str(wins["H2"][1].date()), "variants": holdout_payload["H2"]}
    _write(HOLDOUT["H1"], h1_out)
    _write(HOLDOUT["H2"], h2_out)
    print(f"wrote {HOLDOUT['H1']} and {HOLDOUT['H2']}", flush=True)

    # Per-holdout CSV (all costs)
    ho_rows = []
    for name, payload in (("H1", holdout_payload["H1"]), ("H2", holdout_payload["H2"])):
        for v in variants:
            for cl in costs:
                fm = payload[v]["costs"][cl]
                ho_rows.append({
                    "variant": v, "name": P.VARIANT_NAMES[v], "cost": cl, "window": name,
                    "start": fm["start"], "end": fm["end"],
                    "n_days": fm["n_days"], "n": fm["n"], "n_opened": fm["n_opened"], "n_closed": fm["n_closed"],
                    "Sharpe": fm["Sharpe"], "maxDD": fm["maxDD"], "PF": fm["PF"],
                    "expectancy_R": fm["expectancy_R"], "win_rate": fm["win_rate"], "CAGR": fm["CAGR"],
                    "exposure": fm["exposure"],
                    "killed_before_window": fm.get("killed_before_window", False),
                })
    pd.DataFrame(ho_rows).to_csv(ARTIFACT_DIR / "holdout_per_window.csv", index=False)

    # Final verdicts
    final = {"written_at": ts_saigon(), "variants": {}, "capital": float(capital), "last_closed": str(last_closed.date())}
    for v in variants:
        parts = [prelim[v]["8.1"], prelim[v]["8.2"]]
        for name in ("H1", "H2"):
            m = holdout_payload[name][v]["costs"]["2x"]
            killed_before = bool(m.get("killed_before_window"))
            parts.append(_verdict_holdout(name, m, killed_before))
        overall, why = combine_verdicts(parts)
        final["variants"][v] = {
            "name": P.VARIANT_NAMES[v],
            "overall": overall,
            "first_failed_or_insufficient": why,
            "gates": parts,
        }
    _write(ARTIFACT_DIR / "verdicts.json", final)

    # summary.md
    summary = _render_summary(wf, holdout_payload, final, btc_sharpe_pooled, wins, last_closed, capital, fold_tables, ho_rows)
    (ARTIFACT_DIR / "summary.md").write_text(summary)
    print(f"wrote {ARTIFACT_DIR / 'summary.md'}", flush=True)
    print("OVERALL:", {v: final["variants"][v]["overall"] for v in variants}, flush=True)
    return 0


def _fmt(x, nd=3):
    if x is None:
        return "—"
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return "inf" if x > 0 else ("-inf" if x < 0 else "nan")
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _pct(x):
    if x is None:
        return "—"
    return f"{100*x:.2f}%"


def _render_summary(wf, holdouts, final, btc_sharpe, wins, last_closed, capital, fold_tables, ho_rows) -> str:
    lines = []
    lines.append("# Strategy spec v1 — evaluation report")
    lines.append("")
    lines.append(f"- Written at: {ts_saigon()}")
    lines.append(f"- Capital `C` (allocated_capital): {capital:.2f} USDT (from `/workspace/trading-bot/config.yaml`). The 6,788 USDT main-account wedding funds were **not** used and are excluded.")
    lines.append(f"- Last closed 1Dutc: {pd.Timestamp(last_closed).date()} (H2 end).")
    lines.append(f"- Survivorship: {P.SPEC_V1_PARAMS['survivorship']}")
    lines.append("- Mode: paper/backtest only. No live orders, no API keys, public cache only.")
    lines.append(f"- Reproduce: `cd /workspace/trading-bot && .venv/bin/python -m tradingbot.eval.run_spec_v1`")
    lines.append(f"- Tests: `cd /workspace/trading-bot && .venv/bin/python -m pytest -q`")
    lines.append("")
    lines.append("## Overall verdict (all §8 gates)")
    lines.append("")
    lines.append("| Variant | Name | Overall | First failed / insufficient gate |")
    lines.append("|---|---|---|---|")
    for v in P.VARIANT_IDS:
        o = final["variants"][v]
        lines.append(f"| {v} | {o['name']} | **{o['overall']}** | {o['first_failed_or_insufficient'] or '—'} |")
    lines.append("")
    lines.append("PASS is not an edge claim and is not permission to go live. If no variant meets every bullet, the written outcome is **none passed**.")
    lines.append("")
    lines.append("## Per-criterion verdicts")
    lines.append("")
    lines.append("| Variant | 8.1 folds (2×) | 8.2 pooled OOS (2× + stress, DSR) | 8.3 H1 (2×) | 8.3 H2 (2×) |")
    lines.append("|---|---|---|---|---|")
    for v in P.VARIANT_IDS:
        gates = {g["gate"]: g for g in final["variants"][v]["gates"]}
        def cell(key):
            g = gates[key]
            extra = f" — {g.get('reason')}" if g.get("reason") else ""
            return f"{g['result']}{extra}"
        lines.append(
            f"| {v} | {cell('8.1_folds')} | {cell('8.2_pooled')} | {cell('8.3_H1')} | {cell('8.3_H2')} |"
        )
    lines.append("")
    lines.append("## Per-fold trade counts and metrics (do not hide thin folds behind a pooled n)")
    lines.append("")
    lines.append("Official **n** = completed round-trips whose **exit** falls in the window. **n_opened** = names opened in the window. A fold is evaluable at 2× only if n_days ≥ 60 and n ≥ 10.")
    lines.append("")
    lines.append("| Variant | Cost | Window | n_days | n (closed) | n_opened | Sharpe | maxDD | PF | exp R | evaluable@2× |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in fold_tables:
        ev = "yes" if r["evaluable_2x"] else ("—" if r["cost"] != "2x" else "no")
        lines.append(
            f"| {r['variant']} | {r['cost']} | {r['window']} | {r['n_days']} | {r['n']} | {r['n_opened']} | "
            f"{_fmt(r['Sharpe'])} | {_pct(r['maxDD'])} | {_fmt(r['PF'], 2)} | {_fmt(r['expectancy_R'])} | {ev} |"
        )
    lines.append("")
    lines.append("## Pooled OOS 2023-01-01–2024-12-31")
    lines.append("")
    lines.append(f"BTC-USDT buy-and-hold Sharpe (1× costs, same window): **{_fmt(btc_sharpe)}**.")
    lines.append("")
    lines.append("| Variant | Cost | n | n_opened | Sharpe | maxDD | PF | exp R | CAGR | DSR N=3 | DSR N=38 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in P.VARIANT_IDS:
        for cl in P.COST_LEVELS:
            m = wf["variants"][v]["costs"][cl]["POOLED_OOS"]
            d3 = d38 = "—"
            if cl == "2x":
                d3 = _fmt(wf["variants"][v]["DSR_2x_pooled"]["N3"]["DSR"])
                d38 = _fmt(wf["variants"][v]["DSR_2x_pooled"]["N38"]["DSR"])
            lines.append(
                f"| {v} | {cl} | {m['n']} | {m['n_opened']} | {_fmt(m['Sharpe'])} | {_pct(m['maxDD'])} | "
                f"{_fmt(m['PF'], 2)} | {_fmt(m['expectancy_R'])} | {_pct(m['CAGR'])} | {d3} | {d38} |"
            )
    lines.append("")
    lines.append(f"research4 PBO (context only, different family): {P.RESEARCH4_PBO}.")
    lines.append("")
    lines.append("## Buffer 2025-01-01–2025-03-19 (descriptive only, not a PASS gate)")
    lines.append("")
    lines.append("| Variant | Cost | n | n_opened | Sharpe | maxDD | PF | exp R |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for v in P.VARIANT_IDS:
        for cl in P.COST_LEVELS:
            m = wf["variants"][v]["costs"][cl]["BUFFER"]
            lines.append(
                f"| {v} | {cl} | {m['n']} | {m['n_opened']} | {_fmt(m['Sharpe'])} | {_pct(m['maxDD'])} | "
                f"{_fmt(m['PF'], 2)} | {_fmt(m['expectancy_R'])} |"
            )
    lines.append("")
    lines.append("## Holdouts (run once after lock)")
    lines.append("")
    lines.append("| Variant | Cost | Window | n_days | n (closed) | n_opened | Sharpe | maxDD | PF | exp R | killed_before |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in ho_rows:
        lines.append(
            f"| {r['variant']} | {r['cost']} | {r['window']} {r['start']}–{r['end']} | {r['n_days']} | {r['n']} | {r['n_opened']} | "
            f"{_fmt(r['Sharpe'])} | {_pct(r['maxDD'])} | {_fmt(r['PF'], 2)} | {_fmt(r['expectancy_R'])} | {r['killed_before_window']} |"
        )
    lines.append("")
    lines.append("## Ambiguities and how they were resolved")
    lines.append("")
    lines.append("See the same list at the end of this file in the evaluation notes (also stored in `wf_metrics.json`).")
    for n in wf.get("notes", []):
        lines.append(f"- {n}")
    lines.append("- Official **n** for §8 floors is completed round-trips exiting in the window (same notion as research4 / paper-book PROMOTE). Openings are reported alongside so a pooled n cannot hide a thin fold. This does **not** count a vol-band tweak as a trade, and it does **not** count opening+exit as two trades toward the floor (that would loosen the gate).")
    lines.append("- V-A mid-stop uses the last 20 *closes including t*, matching production `a4_soozie`. V-B/V-C Donchian uses highs/lows on `[t-n, t-1]` as specified.")
    lines.append("- Evaluation time for F2 is the 1Dutc bar close (open + 1 day, 00:00 UTC). HALT iff that instant ∈ [τ−24h, τ+24h).")
    lines.append("- F2 calendar: copied from `/workspace/crypto/reversal-v2/raw/events/events_2022_2026.csv` (not modified) and extended with official October-2026 CPI (2026-11-10 08:30 ET, BLS USDL-26-1685) plus scheduled PPI 2026-11-13, CPI 2026-12-10, and FOMC statement 2026-12-09 (14:00 ET assumed, same convention as the source calendar). Fetch stamp 2026-11-11 (Asia/Saigon), within the 7-day freshness rule.")
    lines.append("- Unscheduled FOMC meetings are out of v1 and were not added.")
    lines.append("- Weights are fractions of *current* book equity (compounding), capped at 10% name / 100% gross. C is the required allocated_capital.")
    lines.append("- V-C: each sleeve starts with a 4% vol budget (1/3 of 12%); 1% risk cap is applied per sleeve vs that sleeve's mid-stop; the 10% name cap and 12% portfolio-vol cap apply to the summed sleeves.")
    lines.append("- Start-of-path: flat at 1.0×C on 2023-01-01. Kill-switch and daily-loss cap reset at the start of this continuous path (not inherited from un-scored warmup).")
    lines.append("- Holdouts were not used to select or retune anything. The lock file was written after WF metrics and before holdout fills.")
    lines.append("")
    lines.append("## Artifact paths")
    lines.append("")
    for p in [
        "wf_metrics.json", "wf_per_fold.csv", "lock.json",
        "holdout_H1.json", "holdout_H2.json", "holdout_per_window.csv",
        "verdicts.json", "summary.md",
    ]:
        lines.append(f"- `/workspace/sprint/artifacts/spec_v1/{p}`")
    lines.append("- `/workspace/sprint/artifacts/spec_v1_lock.json` (copy of the lock)")
    lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HoldoutAlreadyRun as e:
        print(e, file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
