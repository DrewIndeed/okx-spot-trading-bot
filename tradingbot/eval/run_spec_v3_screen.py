#!/usr/bin/env python3
"""v3 baseline replication + official screening (2023-01-01–2024-12-31, 2x).

DATA ALREADY SEEN — cannot count toward PASS. No forward paper runner is started.
No 2025–2026 window is used to allocate slots.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from tradingbot.config import load_config
from tradingbot.eval.engine import run_path
from tradingbot.eval.load import load_panel as load_panel_frozen
from tradingbot.eval.metrics import daily_sharpe_raw, dsr_from_returns, sharpe_daily_series
from tradingbot.eval.regime import build_regime
from tradingbot.eval.v2_engine import curve_series, run_bench_path, run_core_path
from tradingbot.eval.v2_paper import c_paper_from
from tradingbot.eval.v3_engine import run_generic_path
from tradingbot.eval.v3_funding import crowding_flags, load_archive_series
from tradingbot.eval.v3_hypotheses import HYPOTHESIS_IDS, NAMES, build_hypotheses, params_for
from tradingbot.eval.v3_load import load_panel as load_panel_v3
from tradingbot.eval.v3_metrics import (
    MC_DEFAULT_N,
    MC_SEED,
    monte_carlo_trade_order,
    regime_breakdown,
    regime_labels,
    summarize,
    trades_from_book,
)
from tradingbot.eval.v3_protocol import (
    ARTIFACT,
    BRIEF_PATH,
    BRIEF_SHA256_EXPECTED,
    C_PAPER,
    COST_PRIMARY,
    COST_REPORT,
    N_CONSERVATIVE,
    N_OFFICIAL,
    SANITY_END,
    SANITY_START,
    SCREEN_END,
    SCREEN_START,
    allocate_slots,
    assert_brief_hash,
    assert_scores_are_screening_only,
    screening_pass,
    sha256_file,
    sha256_params,
    sha256_paths,
    ts_saigon,
    write_freeze,
)
from tradingbot.strategies.spec_v2_core import core_signals
from tradingbot.universe import load_favorites, monthly_top20_universe

ROOT = Path("/workspace/trading-bot-v3")
CACHE = ROOT / "data" / "cache"
CFG_PATH = ROOT / "config.yaml"
V1_WF = Path("/workspace/sprint/artifacts/spec_v1/wf_metrics.json")
V2_SANITY = Path(
    "/workspace/sprint/artifacts/spec_v2/DATA_ALREADY_SEEN__not_counted_toward_PASS__v2_core_sanity.json"
)
BANNER = "DATA ALREADY SEEN — screening only, not counted toward PASS"


def _metrics(book, start=None, end=None, labels=None) -> dict:
    eq, rets = curve_series(book)
    expo = pd.Series([r.get("exposure", 0.0) for r in book.equity_curve], index=eq.index) if len(eq) else pd.Series(dtype=float)
    turn = pd.Series([r.get("turnover", 0.0) for r in book.equity_curve], index=eq.index) if len(eq) else pd.Series(dtype=float)
    trades = trades_from_book(book)
    m = summarize(eq, rets, trades, expo, turn, start=start, end=end)
    m["killed_on"] = book.killed_on
    m["rearmed_on"] = list(book.rearmed_on)
    if start is not None:
        mask = (rets.index >= start) & (rets.index <= end)
        r_s, e_s, x_s = rets.loc[mask], eq.loc[mask], expo.reindex(rets.index).loc[mask]
        pnls = [
            float(t["pnl_frac"])
            for t in trades
            if t.get("exit_date") and start <= pd.Timestamp(t["exit_date"]) <= end and t.get("pnl_frac") is not None
        ]
    else:
        r_s, e_s, x_s = rets, eq, expo
        pnls = [float(t["pnl_frac"]) for t in trades if t.get("pnl_frac") is not None]
    m["sr_daily"] = daily_sharpe_raw(r_s)
    m["mc"] = monte_carlo_trade_order(pnls, C_PAPER, n=MC_DEFAULT_N, seed=MC_SEED)
    if labels is not None and len(r_s):
        m["regime_breakdown"] = regime_breakdown(r_s, labels, x_s)
    return m, eq, rets


def _match(got: float, expected: float, tol: float = 1e-6) -> dict:
    return {
        "got": got,
        "expected": expected,
        "abs_diff": abs(got - expected),
        "match": abs(got - expected) <= tol,
    }


def run_baseline(capital: float) -> dict:
    """Replicate V2-CORE and 50/50 on the frozen official cache + frozen code."""
    print("baseline: frozen load_panel + run_core_path / run_bench_path", flush=True)
    panel = load_panel_frozen()
    regime = build_regime(panel)
    pre = core_signals(panel)
    start, end = SANITY_START, min(pd.Timestamp(panel["last_closed"]), SANITY_END)
    core = run_core_path(panel, pre, regime, capital, start, end)
    bench = run_bench_path(panel, capital, start, end)
    ceq, cret = curve_series(core)
    beq, bret = curve_series(bench)
    from tradingbot.eval.metrics import max_drawdown

    report = {
        "banner": BANNER,
        "window": [str(start.date()), str(end.date())],
        "last_closed": str(pd.Timestamp(panel["last_closed"]).date()),
        "capital": capital,
        "loader": "frozen tradingbot.eval.load.load_panel (official /workspace/trading-bot JSON cache)",
        "V2-CORE": {
            "Sharpe": sharpe_daily_series(cret),
            "maxDD": max_drawdown(ceq),
            "end_equity": float(ceq.iloc[-1]) if len(ceq) else None,
            "n_days": int(len(cret)),
            "n_closed_trades": len(core.trades_closed),
            "killed_on": core.killed_on,
            "rearmed_on": core.rearmed_on,
        },
        "BENCH-50-50": {
            "Sharpe": sharpe_daily_series(bret),
            "maxDD": max_drawdown(beq),
            "end_equity": float(beq.iloc[-1]) if len(beq) else None,
            "n_days": int(len(bret)),
        },
    }
    expected = json.loads(V2_SANITY.read_text()) if V2_SANITY.exists() else None
    if expected:
        report["replication"] = {
            "V2-CORE_Sharpe": _match(report["V2-CORE"]["Sharpe"], expected["V2-CORE"]["Sharpe"], tol=1e-6),
            "V2-CORE_maxDD": _match(report["V2-CORE"]["maxDD"], expected["V2-CORE"]["maxDD"], tol=1e-8),
            "V2-CORE_end_equity": _match(report["V2-CORE"]["end_equity"], expected["V2-CORE"]["end_equity"], tol=1e-6),
            "BENCH_Sharpe": _match(report["BENCH-50-50"]["Sharpe"], expected["BENCH-50-50"]["Sharpe"], tol=1e-6),
            "BENCH_maxDD": _match(report["BENCH-50-50"]["maxDD"], expected["BENCH-50-50"]["maxDD"], tol=1e-8),
            "BENCH_end_equity": _match(report["BENCH-50-50"]["end_equity"], expected["BENCH-50-50"]["end_equity"], tol=1e-6),
            "expected_window": expected.get("window"),
            "our_window": report["window"],
        }
        mismatches = {k: v for k, v in report["replication"].items() if isinstance(v, dict) and not v.get("match")}
        report["replication"]["all_match"] = not mismatches
        if mismatches:
            report["replication"]["mismatches"] = mismatches
    print("  V2-CORE Sharpe {Sharpe:.6f} maxDD {maxDD:.6f} n={n_closed_trades}".format(**report["V2-CORE"]), flush=True)
    print("  BENCH    Sharpe {Sharpe:.6f} maxDD {maxDD:.6f}".format(**report["BENCH-50-50"]), flush=True)
    return report


def run_vc_frozen(panel, regime, pre, capital, start, end, cost=COST_PRIMARY):
    """Frozen V-C path (v1 engine, sticky kill as in v1). Comparator only."""
    return run_path("V-C", "2x", panel, pre, regime, capital, start, end)


def v1_daily_sharpes() -> dict:
    d = json.loads(V1_WF.read_text())["dsr"]["daily_sharpes"]
    return {k: float(v) for k, v in d.items()}


def main() -> int:
    brief_hash = assert_brief_hash()
    print(f"{BANNER}\nbrief SHA256 {brief_hash}", flush=True)
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / "data").mkdir(parents=True, exist_ok=True)

    cfg = load_config(str(CFG_PATH))
    capital = c_paper_from(cfg)
    if abs(float(capital) - 6788) < 1e-9:
        raise SystemExit("C_paper equals excluded wedding funds; refuse.")
    if float(capital) != float(C_PAPER):
        print(f"note: C_paper from config is {capital}, protocol constant is {C_PAPER}", flush=True)

    baseline = run_baseline(capital)
    (ARTIFACT / "baseline_replication.json").write_text(json.dumps(baseline, indent=2, default=str))

    # --- official screening panel (v3 cache, favorites only) ---
    print("loading v3 1Dutc panel (favorites, parquet, confirm==1)", flush=True)
    panel = load_panel_v3(CACHE, bases=load_favorites(), bar="1Dutc")
    last = pd.Timestamp(panel["last_closed"])
    print(f"  bases={len(panel['bases'])} last_closed={last.date()} (2025+ not used for selection)", flush=True)
    start, end = SCREEN_START, SCREEN_END
    assert_scores_are_screening_only(start, end)

    regime = build_regime(panel)
    labels = regime_labels(panel["close"]["BTC"])
    U = monthly_top20_universe(panel["close"], panel["vq"])
    funding = load_archive_series()
    crowd = crowding_flags(funding, panel["close"].index)
    n_btc_crowd = int(crowd.loc[start:end, "btc_crowd"].sum()) if len(crowd) else 0
    n_eth_crowd = int(crowd.loc[start:end, "eth_crowd"].sum()) if len(crowd) else 0
    n_btc_skip = int(crowd.loc[start:end, "btc_skip"].sum()) if len(crowd) else 0
    print(f"  funding BTC {funding.get('BTC-USDT-SWAP', pd.Series()).index.min()} → {funding.get('BTC-USDT-SWAP', pd.Series()).index.max()}", flush=True)
    print(f"  screening F2 HALT days={int(regime.loc[start:end, 'f2_halt'].sum())} F1 REDUCE={int(regime.loc[start:end, 'f1_reduce'].sum())}", flush=True)
    print(f"  H1 crowd days BTC={n_btc_crowd} ETH={n_eth_crowd} BTC stale-skip={n_btc_skip}", flush=True)

    # Comparators on the SAME screening window (not used as slot fillers if they 'pass')
    print("comparators V2-CORE / BENCH / V-C (frozen) on screening window", flush=True)
    pre_core = core_signals(panel)
    core_s = run_core_path(panel, pre_core, regime, capital, start, end, cost=COST_PRIMARY)
    bench_s = run_bench_path(panel, capital, start, end, cost=COST_PRIMARY)
    core_m, _, core_rets = _metrics(core_s, labels=labels)
    bench_m, _, bench_rets = _metrics(bench_s, labels=labels)
    print("  precompute frozen V-C signals (favorites only, no V-A/V-B) …", flush=True)
    from tradingbot.eval.v3_hypotheses import precompute_vc
    pre_vc = precompute_vc(panel)
    vc_res = run_vc_frozen(panel, regime, pre_vc, capital, start, end, cost=COST_PRIMARY)
    vc_trades = [
        {"symbol": getattr(t, "symbol", None), "exit_date": str(getattr(t, "exit_date", "") or ""), "pnl_frac": getattr(t, "pnl_frac", None)}
        if not isinstance(t, dict)
        else t
        for t in (vc_res.trades or [])
    ]
    vc_m = summarize(vc_res.equity, vc_res.rets, vc_trades, vc_res.exposure, vc_res.turnover)
    vc_m["sr_daily"] = daily_sharpe_raw(vc_res.rets)
    vc_m["killed_on"] = str(vc_res.killed_on) if vc_res.killed_on is not None else None
    print(f"  V2-CORE screen Sharpe={core_m['Sharpe']:.3f} maxDD={core_m['maxDD']:.2%} n={core_m['n_closed_trades']}", flush=True)
    print(f"  BENCH    screen Sharpe={bench_m['Sharpe']:.3f} maxDD={bench_m['maxDD']:.2%}", flush=True)
    print(f"  V-C      screen Sharpe={vc_m['Sharpe']:.3f} maxDD={vc_m['maxDD']:.2%} n={vc_m['n_closed_trades']}", flush=True)

    results = {}
    stress = {}
    precomputed = {}
    for hid in HYPOTHESIS_IDS:
        h0 = build_hypotheses(panel, regime=regime, crowd=crowd, pit_universe=U)[hid]
        print(f"precompute {hid} {h0.name} …", flush=True)
        if hid in ("H1", "H4", "H5") and "H1" in precomputed:
            precomputed[hid] = precomputed["H1"]
            if hid == "H4":
                # H4 target closes over vol_gate; pre can be shared
                pass
        else:
            precomputed[hid] = h0.precompute(panel)
        symbols = list(h0.symbols) if h0.symbols else list(panel["close"].columns)
        # Primary 2x — rebuild hyp so H2 weekly state starts clean
        h = build_hypotheses(panel, regime=regime, crowd=crowd, pit_universe=U)[hid]
        print(f"  run {hid} 2x 40bps {start.date()}–{end.date()}", flush=True)
        book = run_generic_path(
            f"{hid}-{h.name}",
            symbols,
            h.target_fn,
            panel,
            precomputed[hid],
            regime,
            capital,
            start,
            end,
            cost=COST_PRIMARY,
            use_band=h.use_band,
            band_membership_only=h.band_membership_only,
        )
        m, _, rets = _metrics(book, labels=labels)
        results[hid] = {"metrics": m, "rets": rets, "params": h.params, "name": h.name}

        # Report-only stress costs (not used for selection)
        stress[hid] = {}
        for label, cost in COST_REPORT.items():
            if cost == COST_PRIMARY:
                stress[hid][label] = {k: m[k] for k in ("Sharpe", "maxDD", "n_closed_trades", "PF", "end_equity", "total_return") if k in m}
                continue
            print(f"  run {hid} {label} (report only)", flush=True)
            h2 = build_hypotheses(panel, regime=regime, crowd=crowd, pit_universe=U)[hid]
            b2 = run_generic_path(
                f"{hid}-{h.name}-{label}",
                symbols,
                h2.target_fn,
                panel,
                precomputed[hid],
                regime,
                capital,
                start,
                end,
                cost=cost,
                use_band=h.use_band,
                band_membership_only=h.band_membership_only,
            )
            m2, _, _ = _metrics(b2)
            stress[hid][label] = {k: m2[k] for k in ("Sharpe", "maxDD", "n_closed_trades", "PF", "end_equity", "total_return") if k in m2}

    # DSR N=10 / N=45: variance of daily Sharpes across the 10 official trials
    daily = v1_daily_sharpes()  # V-A, V-B, V-C on 2023-2024 pooled (v1)
    daily["V2-CORE"] = float(core_m["sr_daily"])
    daily["V2-ZARA-FROZEN"] = float(vc_m["sr_daily"])
    for hid in HYPOTHESIS_IDS:
        daily[hid] = float(results[hid]["metrics"]["sr_daily"])
    if len(daily) != N_OFFICIAL:
        print(f"warning: daily Sharpe count {len(daily)} != N={N_OFFICIAL}: {list(daily)}", flush=True)
    sr_var = float(pd.Series(daily).var(ddof=1)) if len(daily) > 1 else 0.0

    screen_rows = []
    for hid in HYPOTHESIS_IDS:
        m = results[hid]["metrics"]
        passed, reason = screening_pass(hid, int(m["n_closed_trades"]), float(m["Sharpe"]), float(m["maxDD"]))
        d10 = dsr_from_returns(results[hid]["rets"], sr_var, N_OFFICIAL)
        d45 = dsr_from_returns(results[hid]["rets"], sr_var, N_CONSERVATIVE)
        row = {
            "id": hid,
            "name": results[hid]["name"],
            "window": [str(start.date()), str(end.date())],
            "cost": "2x",
            "cost_per_side": COST_PRIMARY,
            "Sharpe": m["Sharpe"],
            "maxDD": m["maxDD"],
            "n_closed_trades": m["n_closed_trades"],
            "PF": m["PF"],
            "end_equity": m["end_equity"],
            "total_return": m["total_return"],
            "CAGR": m["CAGR"],
            "mean_exposure": m["mean_exposure"],
            "mean_turnover": m["mean_turnover"],
            "n_days": m["n_days"],
            "killed_on": m.get("killed_on"),
            "screen_pass": passed,
            "first_failed_gate": reason,
            "DSR_N10": d10,
            "DSR_N45": d45,
            "mc": m["mc"],
            "regime_breakdown": m.get("regime_breakdown"),
            "stress": stress[hid],
            "vs_V2-CORE_Sharpe": m["Sharpe"] - core_m["Sharpe"],
            "vs_BENCH_maxDD": m["maxDD"] - bench_m["maxDD"],
        }
        screen_rows.append(row)
        flag = "PASS-screen" if passed else f"FAIL-screen ({reason})"
        print(f"  {hid} {row['name']}: Sharpe={m['Sharpe']:.3f} maxDD={m['maxDD']:.2%} n={m['n_closed_trades']} DSR10={d10['DSR']:.3f} {flag}", flush=True)

    slots = allocate_slots(screen_rows)
    slot_ids = [s["id"] for s in slots]
    print(f"forward slots ({len(slot_ids)}/{3}): {slot_ids}", flush=True)

    out = {
        "banner": BANNER,
        "recorded_at": ts_saigon(),
        "sha256_brief": brief_hash,
        "brief_path": str(BRIEF_PATH),
        "screening_window": [str(start.date()), str(end.date())],
        "capital": capital,
        "N_official": N_OFFICIAL,
        "N_conservative": N_CONSERVATIVE,
        "sr_trials_var": sr_var,
        "daily_sharpes_for_DSR": daily,
        "funding": {
            "source": "OKX official archive module 3 (monthly zips) plus public REST ~3 months (2026 only, not used in this window)",
            "parse": "funding_time is a Unix epoch instant; module 3 file months are UTC+8 (2023-01 zip starts 2022-12-31 16:00 UTC = 2023-01-01 00:00 UTC+8). No extra 8h shift.",
            "btc_range": [str(funding["BTC-USDT-SWAP"].index.min()), str(funding["BTC-USDT-SWAP"].index.max())] if "BTC-USDT-SWAP" in funding else None,
            "eth_range": [str(funding["ETH-USDT-SWAP"].index.min()), str(funding["ETH-USDT-SWAP"].index.max())] if "ETH-USDT-SWAP" in funding else None,
            "n_prints_btc": int(len(funding.get("BTC-USDT-SWAP", []))),
            "n_prints_eth": int(len(funding.get("ETH-USDT-SWAP", []))),
            "screening_btc_crowd_days": n_btc_crowd,
            "screening_eth_crowd_days": n_eth_crowd,
            "screening_btc_stale_skip_days": n_btc_skip,
            "2023_01_btc_zip": "copied from /workspace/crypto/reversal-v2/raw/funding/BTC_2023-01.zip (not re-downloaded)",
        },
        "comparators_screening_2x": {"V2-CORE": core_m, "BENCH-50-50": bench_m, "V-C-frozen": vc_m},
        "hypotheses": screen_rows,
        "forward_slots": slot_ids,
        "baseline_replication_path": str(ARTIFACT / "baseline_replication.json"),
        "note": "Slots ranked by 2x Sharpe then shallower maxDD among screening passes only. Failed hypotheses are not used as fillers. No 2025–2026 peek was used. No forward paper runner was started. v2 official runner is untouched.",
    }
    (ARTIFACT / "screening.json").write_text(json.dumps(out, indent=2, default=str))

    # CSV
    import csv
    csv_path = ARTIFACT / "screening.csv"
    fields = [
        "id", "name", "screen_pass", "first_failed_gate", "Sharpe", "maxDD",
        "n_closed_trades", "PF", "end_equity", "total_return", "mean_exposure",
        "mean_turnover", "DSR_N10", "DSR_N45", "vs_V2-CORE_Sharpe", "vs_BENCH_maxDD",
    ]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in screen_rows:
            w.writerow({
                **{k: r.get(k) for k in fields if k not in ("DSR_N10", "DSR_N45")},
                "DSR_N10": r["DSR_N10"]["DSR"],
                "DSR_N45": r["DSR_N45"]["DSR"],
            })

    # Freeze hashes for brief + each candidate (and all 5 params/code even if not slotted)
    code_hash = sha256_paths()
    candidates = []
    for hid in HYPOTHESIS_IDS:
        candidates.append({
            "id": hid,
            "name": NAMES[hid],
            "slotted": hid in slot_ids,
            "sha256_params": sha256_params(params_for(hid)),
            "params": params_for(hid),
        })
    freeze_payload = {
        "t0_if_cypheous_signs_off_before_close": "2026-11-12 07:00 UTC+7 (same 1Dutc close as v2); otherwise first 1Dutc close after freeze",
        "sha256_v3_code": code_hash,
        "v3_code_paths": [str(p) for p in __import__("tradingbot.eval.v3_protocol", fromlist=["V3_CODE_PATHS"]).V3_CODE_PATHS],
        "sha256_brief": brief_hash,
        "screening_window": [str(start.date()), str(end.date())],
        "forward_slots": slot_ids,
        "candidates": candidates,
        "N_official": N_OFFICIAL,
        "N_conservative": N_CONSERVATIVE,
        "do_not_start_paper_runner": True,
    }
    freeze_path = write_freeze(freeze_payload)
    print(f"freeze written {freeze_path}", flush=True)
    write_summary(out, baseline, freeze_path)
    write_reproduce()
    print("done.", flush=True)
    return 0


def write_summary(out: dict, baseline: dict, freeze_path: Path) -> None:
    lines = [
        "# Spec v3 screening summary",
        "",
        BANNER,
        "",
        f"Written: {out['recorded_at']}",
        f"Official brief: `{out['brief_path']}` SHA256 `{out['sha256_brief']}`",
        "Cypheous owns strategy acceptance. This is screening on seen data only. **Not a PASS.**",
        "Paper only. No keys, no live orders, no forward runner started. 6,788 USDT wedding funds excluded. C_paper=10000.",
        "",
        "## Honest out-of-sample status",
        "",
        "- 2023-01 → 2026-11 has already been seen (research4, reversal, spec v1 WF + holdouts, v2 sanity).",
        "- Pre-2023 was also used (research4 IS, OOS-2022, warmup). There is no unused historical OKX segment that can grade v3 as PASS.",
        "- Screening used **2023-01-01–2024-12-31 only**, 2× = 40 bps/side. 2025–2026 and v1 holdouts H1/H2 were **not** used to choose slots.",
        "- The only clean OOS is forward paper from v3 T0 (same close as v2, 2026-11-12 07:00 UTC+7, if the freeze is accepted before that close). That runner is **not** started here.",
        "",
        "## Baseline replication (frozen code, official cache)",
        "",
    ]
    rep = baseline.get("replication") or {}
    vc = baseline["V2-CORE"]
    vb = baseline["BENCH-50-50"]
    lines += [
        f"Window {baseline['window'][0]}–{baseline['window'][1]} (v2 sanity overlap), C_paper={baseline['capital']}.",
        f"Loader: {baseline['loader']}.",
        f"- V2-CORE: Sharpe {vc['Sharpe']:.6f}, maxDD {100*vc['maxDD']:.4f}%, n_days {vc['n_days']}, n_trades {vc['n_closed_trades']}, end {vc['end_equity']:.4f}",
        f"- BENCH-50-50: Sharpe {vb['Sharpe']:.6f}, maxDD {100*vb['maxDD']:.4f}%, n_days {vb['n_days']}, end {vb['end_equity']:.4f}",
        f"- Replication vs v2 sanity file: {'MATCH' if rep.get('all_match') else 'MISMATCH — see baseline_replication.json'}.",
        "",
    ]
    if rep and not rep.get("all_match"):
        lines.append("Mismatches:")
        for k, v in (rep.get("mismatches") or {}).items():
            lines.append(f"- {k}: got {v['got']} expected {v['expected']} diff {v['abs_diff']}")
        lines.append("")

    lines += [
        "## Official hypotheses (k=5). Official DSR N=10 (3 v1 + 2 v2 + 5). Also report N=45.",
        "",
        f"sr_trials_var (unannualized daily Sharpes of the 10 trials) = {out['sr_trials_var']:.8f}.",
        "",
        "| ID | Name | n | Sharpe | maxDD | PF | end eq | DSR(N=10) | DSR(N=45) | screen | first failed gate |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in out["hypotheses"]:
        pf = r["PF"]
        pf_s = "—" if pf is None else ("inf" if pf == float("inf") else f"{pf:.2f}")
        lines.append(
            f"| {r['id']} | {r['name']} | {r['n_closed_trades']} | {r['Sharpe']:.3f} | {100*r['maxDD']:.2f}% | "
            f"{pf_s} | {r['end_equity']:.1f} | {r['DSR_N10']['DSR']:.3f} | {r['DSR_N45']['DSR']:.3f} | "
            f"{'PASS' if r['screen_pass'] else 'FAIL'} | {r['first_failed_gate'] or '—'} |"
        )
    lines += [
        "",
        "A hypothesis passes screening only if n≥30 (≥8 for H3), Sharpe>0, and maxDD shallower than −15%, all at 2× on 2023–2024.",
        "",
        "## Forward slots (≤3)",
        "",
    ]
    if out["forward_slots"]:
        lines.append(", ".join(out["forward_slots"]) + " (ranked by 2× Sharpe, then shallower maxDD).")
        lines.append("Do not start a paper runner until Cypheous signs off on this screening result and the freeze hashes.")
    else:
        lines.append("**None.** No hypothesis passed screening. v3 adds no forward book; v2 paper continues. Leftover slots are not filled.")
    cmp_ = out["comparators_screening_2x"]
    lines += [
        "",
        "## Comparators on the same 2023–2024 window (2×), not slot candidates",
        "",
        f"- V2-CORE: Sharpe {cmp_['V2-CORE']['Sharpe']:.3f}, maxDD {100*cmp_['V2-CORE']['maxDD']:.2f}%, n={cmp_['V2-CORE']['n_closed_trades']}",
        f"- 50/50 BTC/ETH monthly: Sharpe {cmp_['BENCH-50-50']['Sharpe']:.3f}, maxDD {100*cmp_['BENCH-50-50']['maxDD']:.2f}%",
        f"- V-C frozen (v1 engine, sticky kill): Sharpe {cmp_['V-C-frozen']['Sharpe']:.3f}, maxDD {100*cmp_['V-C-frozen']['maxDD']:.2f}%, n={cmp_['V-C-frozen']['n_closed_trades']}",
        "",
        "## Funding (H1)",
        "",
        out["funding"]["source"],
        out["funding"]["parse"],
        f"BTC prints: {out['funding']['n_prints_btc']} from {out['funding']['btc_range']}.",
        f"ETH prints: {out['funding']['n_prints_eth']} from {out['funding']['eth_range']}.",
        f"Screening days with BTC crowding: {out['funding']['screening_btc_crowd_days']}; ETH crowding: {out['funding']['screening_eth_crowd_days']}; BTC stale-skip: {out['funding']['screening_btc_stale_skip_days']}.",
        f"{out['funding']['2023_01_btc_zip']}.",
        "",
        "## Freeze",
        "",
        f"Hashes: `{freeze_path}`. No overwrite. No paper runner started from this step.",
        "",
        "Monte Carlo (5,000 trade-order reshuffles, seed 42) and regime breakdowns (BULL/BEAR/CHOP) are in `screening.json`.",
        "Stress costs 1× / 25 bps / 50 bps are reported there; selection used 2× only.",
        "",
    ]
    (ARTIFACT / "summary.md").write_text("\n".join(lines) + "\n")


def write_reproduce() -> None:
    text = """# Reproduce spec v3 screening

Worktree: /workspace/trading-bot-v3  (branch v3). Do not touch /workspace/trading-bot or RUNNING.lock.

    cd /workspace/trading-bot-v3
    .venv/bin/python -m tradingbot.eval.run_spec_v3_screen

Requirements: official brief /workspace/sprint/strategy-v3-brief.md SHA256
6544095cf2e7203935e9741f3bfaef139afc3fddee0676f3c6b364f7dbbd72d2
must be unchanged. Screening window is hard-coded 2023-01-01–2024-12-31, 2× 40 bps/side.
C_paper=10000. Artifacts write to /workspace/sprint/artifacts/spec_v3/.
freeze.json is write-once. Do not start a paper runner from this script.

Funding zips live in data/funding/ (gitignored). The 2023-01 BTC zip is the local probe copy, not a re-download.
"""
    (ARTIFACT / "REPRODUCE.txt").write_text(text)


if __name__ == "__main__":
    raise SystemExit(main())
