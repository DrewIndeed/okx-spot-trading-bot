"""Spec v4 research runner (paper/backtest only, SPOT long-only).

Pre-registered (committed before any v4 result was computed):
  Base = frozen v3 H5 (F2-SOFT). Six v4 variants, fixed params, no grid:
    V4-TRAIL  trailing 3xATR14 chandelier exit (+ re-entry lockout until base signal resets)
    V4-SCALE  trim to 50% once close >= entry + 3xATR14(entry)
    V4-TIME   exit if held >= 30 sessions and close <= entry
    V4-BUYTR  new entries only if BTC > SMA100 and name > SMA50
    V4-VOLX   BTC sigma10 > 1.5x trailing-365d median -> halve weights, block increases
    V4-COMBO  BUYTR + TRAIL + VOLX
  Honest trial count: official N = 10 (v1-v3) + 6 = 16; conservative N = 45 + 6 = 51.
  Phase wf (tuning domain only, 2021-07-01..2026-11-10, purged walk-forward):
    per test fold, pick the variant (H5 + 6) with the best Sharpe on [2021-07-01, fold_start-30d] at 2x.
    Candidate rule: the v4 variant with the best median test-fold Sharpe at 2x that also beats H5
    in >= 3 of 5 folds; otherwise NO candidate (keep v3). Candidate is written to candidate.json.
  Phase holdout: requires candidate.json; runs once (refuses if holdout results exist).
"""
from __future__ import annotations

import argparse, hashlib, json, subprocess, sys
from pathlib import Path

import numpy as np
import pandas as pd

from tradingbot.eval.engine import run_path
from tradingbot.eval.metrics import daily_sharpe_raw
from tradingbot.eval.regime import build_regime
from tradingbot.eval.v2_engine import curve_series, run_bench_path, run_core_path
from tradingbot.eval.v3_engine import run_generic_path
from tradingbot.eval.v3_funding import crowding_flags, load_archive_series
from tradingbot.eval.v3_hypotheses import build_hypotheses, precompute_vc
from tradingbot.eval.v3_load import load_panel
from tradingbot.eval.v3_metrics import dsr_for, monte_carlo_trade_order, summarize, trades_from_book
from tradingbot.eval.v4_exits import V4Config, make_v4_target, run_v4_path
from tradingbot.strategies.spec_v2_core import core_signals
from tradingbot.universe import load_favorites, monthly_top20_universe

ROOT = Path("/workspace/trading-bot-v4")
ART = Path("/workspace/sprint/artifacts/spec_v4")
C_PAPER = 10000.0
COSTS = {"2x": 0.0040, "1x": 0.0020}
N_OFF, N_CONS = 16, 51
TUNE_START, TUNE_END = pd.Timestamp("2021-07-01"), pd.Timestamp("2026-11-10")
FOLDS = [("2022-01-01", "2022-12-31"), ("2023-01-01", "2023-12-31"), ("2024-01-01", "2024-12-31"),
         ("2025-01-01", "2025-12-31"), ("2026-01-01", "2026-11-10")]
V4 = {
    "V4-TRAIL": V4Config(trail_atr=3.0),
    "V4-SCALE": V4Config(scale_atr=3.0),
    "V4-TIME": V4Config(time_days=30),
    "V4-BUYTR": V4Config(buy_trend=True),
    "V4-VOLX": V4Config(vol_shock=1.5),
    "V4-COMBO": V4Config(trail_atr=3.0, buy_trend=True, vol_shock=1.5),
}
COMPARATORS = ["V2-CORE", "V-C", "H1", "H4", "H5", "BENCH-50-50"]


def setup():
    panel = load_panel(ROOT / "data" / "cache", bases=load_favorites(), bar="1Dutc")
    regime = build_regime(panel)
    U = monthly_top20_universe(panel["close"], panel["vq"])
    crowd = crowding_flags(load_archive_series(Path("/workspace/trading-bot-v3/data/funding")), panel["close"].index)
    return panel, regime, U, crowd


def book_pre(h, panel):
    """Frozen precompute + BTC SMA200 so the v2/v3 kill switch can re-arm (flat 30d, then BTC > SMA200).
    precompute_vc lacks it, so in v3 harnesses a killed H1/H4/H5 book could never re-arm."""
    from tradingbot.universe import sma
    return {**h.precompute(panel), "sma200_btc": sma(panel["close"]["BTC"], 200)}


def run_book(name, ctx, start, end, cost):
    panel, regime, U, crowd = ctx
    if name == "V2-CORE":
        b = run_core_path(panel, core_signals(panel), regime, C_PAPER, start, end, cost=cost)
    elif name == "BENCH-50-50":
        b = run_bench_path(panel, C_PAPER, start, end, cost=cost)
    elif name == "V-C":
        r = run_path("V-C", "2x" if cost == 0.004 else "1x", panel, precompute_vc(panel), regime, C_PAPER, start, end)
        tr = [t if isinstance(t, dict) else {"exit_date": str(getattr(t, "exit_date", "") or "") or None, "pnl_frac": getattr(t, "pnl_frac", None)} for t in (r.trades or [])]
        return r.equity, r.rets, tr, r.exposure, r.turnover
    else:
        hs = build_hypotheses(panel, regime=regime, crowd=crowd, pit_universe=U)
        h = hs["H5" if name.startswith("V4") else name]
        pre = book_pre(h, panel)
        syms = list(h.symbols) if h.symbols else list(panel["close"].columns)
        if name.startswith("V4"):
            tf = make_v4_target(h.target_fn, V4[name], panel)
            b = run_v4_path(name, syms, tf, panel, pre, regime, C_PAPER, start, end, cost=cost, use_band=h.use_band, band_membership_only=h.band_membership_only)
        else:
            b = run_generic_path(name, syms, h.target_fn, panel, pre, regime, C_PAPER, start, end, cost=cost, use_band=h.use_band, band_membership_only=h.band_membership_only)
    eq, rets = curve_series(b)
    expo = pd.Series([r.get("exposure", 0.0) for r in b.equity_curve], index=eq.index)
    turn = pd.Series([r.get("turnover", 0.0) for r in b.equity_curve], index=eq.index)
    return eq, rets, trades_from_book(b), expo, turn


def metrics(eq, rets, trades, expo, turn, sr_list):
    m = summarize(eq, rets, trades, expo, turn)
    r = rets.dropna()
    dn = r[r < 0]
    m["Sortino"] = float(r.mean() / np.sqrt((dn ** 2).sum() / len(r)) * np.sqrt(365)) if len(dn) else None
    m["turnover_ann"] = float(turn.mean() * 365) if len(turn) else None
    pnls = [float(t["pnl_frac"]) for t in trades if t.get("pnl_frac") is not None and t.get("exit_date")]
    mc = monte_carlo_trade_order(pnls, C_PAPER)
    m["mc_p5_maxDD"] = (mc.get("maxDD") or {}).get("p5")
    m["DSR_N16"] = dsr_for(r, sr_list, N_OFF).get("DSR")
    m["DSR_N51"] = dsr_for(r, sr_list, N_CONS).get("DSR")
    return m


def sharpe(r):
    r = r.dropna()
    return float(r.mean() / r.std(ddof=1) * np.sqrt(365)) if len(r) > 2 and r.std() > 0 else 0.0


def git_sha():
    return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip()


def phase_wf():
    assert (ART / "holdout.json").exists(), "lock holdout.json first"
    ctx = setup()
    names = COMPARATORS + list(V4)
    res = {}
    for n in names:
        print("tune-domain run", n, flush=True)
        res[n] = run_book(n, ctx, TUNE_START, TUNE_END, COSTS["2x"])
        assert res[n][1].index.min() >= TUNE_START, "scored a date before the tuning domain"
    # replication check vs v3 screening (H5 2023-2024 Sharpe 1.406 at 2x)
    rep = run_book("H5", ctx, pd.Timestamp("2023-01-01"), pd.Timestamp("2024-12-31"), COSTS["2x"])
    rep_sh = summarize(rep[0], rep[1], rep[2], rep[3], rep[4])["Sharpe"]
    pool = ["H5"] + list(V4)
    fold_rows, wf_chosen, wf_rets = [], [], []
    for fs, fe in FOLDS:
        fs, fe = pd.Timestamp(fs), pd.Timestamp(fe)
        tr_end = fs - pd.Timedelta(days=30)
        best = max(pool, key=lambda n: sharpe(res[n][1].loc[TUNE_START:tr_end]))
        wf_chosen.append(best)
        wf_rets.append(res[best][1].loc[fs:fe])
        row = {"fold": f"{fs.date()}..{fe.date()}", "wf_pick": best}
        for n in names:
            row[n] = sharpe(res[n][1].loc[fs:fe])
        fold_rows.append(row)
    folds = pd.DataFrame(fold_rows)
    beats = {n: int((folds[n] > folds["H5"]).sum()) for n in V4}
    med = {n: float(folds[n].median()) for n in V4}
    elig = [n for n in V4 if beats[n] >= 3]
    cand = max(elig, key=lambda n: med[n]) if elig else None
    wf_series = pd.concat(wf_rets)
    sr_list = [daily_sharpe_raw(res[n][1]) for n in names if n != "BENCH-50-50"]
    full = {}
    for n in names:
        full[n] = metrics(*res[n], sr_list)
    out = {"folds": fold_rows, "beats_H5_folds": beats, "median_fold_sharpe": med, "wf_picks": wf_chosen,
           "wf_selected_oos_sharpe": sharpe(wf_series), "candidate": cand, "replication_H5_2023_2024_sharpe": rep_sh,
           "tuning_domain_full": full, "code_sha": git_sha()}
    (ART / "wf_results.json").write_text(json.dumps(out, indent=1, default=str))
    body = {"candidate": cand, "config": (V4[cand].__dict__ if cand else None), "base": "H5 (frozen v3)", "code_sha": git_sha(),
            "v4_exits_sha256": hashlib.sha256((ROOT / "tradingbot/eval/v4_exits.py").read_bytes()).hexdigest(),
            "rule": "best median WF test-fold Sharpe at 2x among v4 variants that beat H5 in >=3/5 folds"}
    body["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    (ART / "candidate.json").write_text(json.dumps(body, indent=1, default=str))
    print(folds.round(3).to_string(), "\nbeats", beats, "\ncandidate", cand, "replication", round(rep_sh, 3), flush=True)


def phase_holdout():
    ho = json.loads((ART / "holdout.json").read_text())
    assert (ART / "candidate.json").exists(), "candidate must be locked before the holdout"
    out_p = ART / "holdout_results.json"
    if out_p.exists():
        sys.exit("holdout already evaluated once; refusing to re-run")
    s, e = pd.Timestamp(ho["holdout"]["start"]), pd.Timestamp(ho["holdout"]["end"])
    ctx = setup()
    names = COMPARATORS + list(V4)
    rows, raw = [], {}
    for cl, c in COSTS.items():
        for n in names:
            print("holdout run", n, cl, flush=True)
            raw[(n, cl)] = run_book(n, ctx, s, e, c)
            assert raw[(n, cl)][1].index.max() <= e
        sr_list = [daily_sharpe_raw(raw[(n, cl)][1]) for n in names if n != "BENCH-50-50"]
        for n in names:
            m = metrics(*raw[(n, cl)], sr_list)
            rows.append({"window": "holdout", "cost": cl, "book": n, **{k: m.get(k) for k in (
                "CAGR", "Sharpe", "Sortino", "maxDD", "n_closed_trades", "turnover_ann", "mean_exposure", "DSR_N16", "DSR_N51", "mc_p5_maxDD", "end_equity", "start", "end")}})
    pd.DataFrame(rows).to_csv(ART / "holdout_table.csv", index=False)
    out_p.write_text(json.dumps({"rows": rows, "code_sha": git_sha()}, indent=1, default=str))
    print(pd.DataFrame(rows)[["cost", "book", "CAGR", "Sharpe", "maxDD", "n_closed_trades", "DSR_N16"]].round(3).to_string())


def phase_recompute():
    """Deterministic re-run to add metrics only (DSR key fix). Asserts every stored holdout Sharpe/CAGR is
    reproduced bit-for-bit; selection and candidate are not touched. Writes results.csv (tuning domain + WF folds + holdout)."""
    ho = json.loads((ART / "holdout.json").read_text())
    stored = {(r["book"], r["cost"]): r for r in json.loads((ART / "holdout_results.json").read_text())["rows"]}
    s, e = pd.Timestamp(ho["holdout"]["start"]), pd.Timestamp(ho["holdout"]["end"])
    ctx = setup()
    names = COMPARATORS + list(V4)
    rows = []
    for win, (ws, we), costs in (("tuning_2021-07..2026-11", (TUNE_START, TUNE_END), ("2x",)), ("holdout_2018-11..2020-09", (s, e), ("2x", "1x"))):
        for cl in costs:
            raw = {n: run_book(n, ctx, ws, we, COSTS[cl]) for n in names}
            sr_list = [daily_sharpe_raw(raw[n][1]) for n in names if n != "BENCH-50-50"]
            for n in names:
                m = metrics(*raw[n], sr_list)
                if win.startswith("holdout"):
                    old = stored[(n, cl)]
                    assert abs(old["Sharpe"] - m["Sharpe"]) < 1e-12 and abs(old["CAGR"] - m["CAGR"]) < 1e-12, (n, cl)
                rows.append({"window": win, "cost": cl, "book": n, **{k: m.get(k) for k in (
                    "CAGR", "Sharpe", "Sortino", "maxDD", "n_closed_trades", "turnover_ann", "mean_exposure", "DSR_N16", "DSR_N51", "mc_p5_maxDD", "end_equity", "start", "end")}})
                if win.startswith("tuning"):
                    for fs, fe in FOLDS:
                        rows.append({"window": f"wf_fold_{fs[:4]}", "cost": cl, "book": n, "Sharpe": sharpe(raw[n][1].loc[fs:fe])})
    df = pd.DataFrame(rows)
    df.to_csv(ART / "results.csv", index=False)
    print(df[df.window.str.startswith("holdout")][["cost", "book", "Sharpe", "DSR_N16", "DSR_N51", "mc_p5_maxDD"]].round(3).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["wf", "holdout", "recompute"])
    a = ap.parse_args()
    {"wf": phase_wf, "holdout": phase_holdout, "recompute": phase_recompute}[a.phase]()
