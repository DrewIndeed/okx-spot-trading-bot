"""D1 breakout parameter sweep — walk-forward, out-of-sample focused.

Does NOT touch locked strategy files. Tests parameter combinations and reports
only those that pass: OOS profit factor > 1, max drawdown cap, and 2x fee stress.
Not financial advice. Past backtest is not a guarantee of future results.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make repo importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingbot.config import load_config
from tradingbot.data.rest import OkxRestFeed
from tradingbot.universe import (
    inst_id,
    load_favorites,
    panel_from_candles,
    breadth_top20,
    monthly_top20_universe,
    sma,
)
from tradingbot.regime import btc_above_sma200
from tradingbot.types import Candle


def load_panel(cfg, days: int = 1200, update: bool = False):
    feed = OkxRestFeed(
        cache_dir=cfg.data.cache_dir,
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )
    bases = load_favorites(cfg.universe_file)
    by = {}
    missing = []
    for b in bases:
        inst = inst_id(b)
        try:
            cs = feed.get_candles(inst, "1Dutc", days=days, update=update, closed_only=True)
        except Exception:
            cs = feed.get_candles(inst, "1Dutc", days=days, update=False, closed_only=True)
        if cs:
            by[b] = cs
        else:
            missing.append(b)
    if "BTC" not in by:
        raise SystemExit("Need BTC 1Dutc cached. Run: python -m tradingbot data-sync --bars 1Dutc --no-update")
    C, HI, LO, VQ, O = panel_from_candles(by)
    return C, HI, LO, VQ, O, missing


def d1_run(C: pd.DataFrame, HI: pd.DataFrame, LO: pd.DataFrame, VQ: pd.DataFrame,
           lookback: int, breadth_min: float, risk_pct: float, max_weight: float,
           fee: float, slip: float, cap: float,
           n_folds: int = 4, oos_ratio: float = 0.20):
    """Run D1-like logic with adjustable params. Returns metrics dict."""
    n = len(C)
    if n < 250:
        return None

    U = monthly_top20_universe(C, quote_vol=VQ, top_n=20)
    br = breadth_top20(C, U).values
    gate = btc_above_sma200(C["BTC"]).reindex(C.index).fillna(0.0).values

    w = pd.DataFrame(0.0, C.index, C.columns)
    trades = []

    for s in C.columns:
        c = C[s].values
        u = U[s].values
        prevmax = C[s].shift(1).rolling(lookback).max().values
        mx = C[s].rolling(lookback).max().values
        mn = C[s].rolling(lookback).min().values
        pos = False
        st = 0.0
        ep = 0.0
        risk = 0.0
        t0 = None
        size = 0.0
        col = np.zeros(len(c))

        for t in range(len(c)):
            if np.isnan(c[t]):
                pos = False
                continue
            stop_t = (mx[t] + mn[t]) / 2.0
            if pos:
                if not np.isnan(stop_t):
                    st = max(st, stop_t)
                if c[t] < st or gate[t] <= 0:
                    pos = False
                    trades.append({"sym": s, "entry_idx": t0, "exit_idx": t,
                                   "entry": ep, "exit": c[t], "risk": risk, "size": size})
            elif (u[t] and gate[t] > 0 and br[t] >= breadth_min
                  and not np.isnan(prevmax[t]) and c[t] > prevmax[t]
                  and not np.isnan(stop_t) and stop_t < c[t]):
                pos = True
                ep = c[t]
                st = stop_t
                risk = (ep - st) / ep
                t0 = t
                size = min(risk_pct / risk, max_weight)
            col[t] = size if pos else 0.0
        w[s] = col

    # cap_total: scale down if gross > 1
    ssum = w.sum(axis=1)
    w = w.div(np.maximum(ssum, 1.0), axis=0)
    w = w.mul(pd.DataFrame(np.tile(gate > 0, (len(C.columns), 1)).T, index=C.index, columns=C.columns), axis=0)

    # Equity simulation (close-to-close, cost on turnover)
    cost = fee + slip
    equity = [cap]
    eq_ts = [int(pd.Timestamp(C.index[0]).value // 1_000_000)]
    cash_eq = cap
    prev_w = w.iloc[0] * 0.0

    for i in range(1, len(C)):
        ret = (C.iloc[i] / C.iloc[i - 1] - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        gross = float((prev_w * ret).sum())
        turn = float((w.iloc[i] - prev_w).abs().sum())
        net = gross - cost * turn
        cash_eq = cash_eq * (1.0 + net)
        equity.append(cash_eq)
        eq_ts.append(int(pd.Timestamp(C.index[i]).value // 1_000_000))
        prev_w = w.iloc[i]

    equity = np.array(equity)
    # Walk-forward OOS
    oos_total = max(n_folds, int(n * oos_ratio))
    fold = oos_total // n_folds
    start_oos = n - fold * n_folds

    def max_dd(eq):
        peak = eq[0]
        dd = 0.0
        for x in eq:
            if x > peak:
                peak = x
            if peak > 0:
                dd = min(dd, (x - peak) / peak)
        return abs(dd)

    def pf(trades_pnl):
        wins = sum(p for p in trades_pnl if p > 0)
        losses = sum(p for p in trades_pnl if p < 0)
        if losses == 0 and wins > 0:
            return float("inf")
        if losses < 0:
            return wins / abs(losses)
        return None

    # Closed trade PnL from trade records (approx)
    trade_pnls = []
    for tr in trades:
        ei, ex = tr["entry_idx"], tr["exit_idx"]
        if ei is None or ex is None or ei >= len(equity) or ex >= len(equity):
            continue
        r_net = (tr["exit"] / tr["entry"]) * (1.0 - cost) ** 2 - 1.0 if tr["entry"] else 0.0
        e0 = equity[ei] if ei < len(equity) else cap
        trade_pnls.append(r_net * tr["size"] * e0)

    folds = []
    for k in range(n_folds):
        oos_s = start_oos + k * fold
        oos_e = oos_s + fold if k < n_folds - 1 else n
        is_eq = equity[:oos_s]
        oos_eq = equity[oos_s:oos_e]
        # trades whose entry is in OOS window
        oos_pnls = [p for tr, p in zip(trades, trade_pnls)
                    if tr["entry_idx"] is not None and oos_s <= tr["entry_idx"] < oos_e]
        folds.append({
            "fold": k + 1,
            "is_pf": pf(trade_pnls[: max(0, oos_s)]),
            "is_dd": max_dd(is_eq) if len(is_eq) else 0.0,
            "oos_pf": pf(oos_pnls),
            "oos_dd": max_dd(oos_eq) if len(oos_eq) else 0.0,
            "oos_n": len(oos_pnls),
        })

    full_pf = pf(trade_pnls)
    full_dd = max_dd(equity)

    # 2x fee stress equity
    cost2 = fee * 2 + slip * 2
    equity2 = [cap]
    cash_eq2 = cap
    prev_w2 = w.iloc[0] * 0.0
    for i in range(1, len(C)):
        ret = (C.iloc[i] / C.iloc[i - 1] - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        gross = float((prev_w2 * ret).sum())
        turn = float((w.iloc[i] - prev_w2).abs().sum())
        net = gross - cost2 * turn
        cash_eq2 = cash_eq2 * (1.0 + net)
        equity2.append(cash_eq2)
        prev_w2 = w.iloc[i]
    stress_dd = max_dd(np.array(equity2))

    return {
        "lookback": lookback,
        "breadth_min": breadth_min,
        "risk_pct": risk_pct,
        "max_weight": max_weight,
        "full_pf": full_pf,
        "full_dd": full_dd,
        "full_eq": equity[-1],
        "stress_dd": stress_dd,
        "folds": folds,
        "n_trades": len(trades),
    }


def passes(m, max_dd_cap: float = 0.20, min_oos_pf: float = 1.0) -> bool:
    if m is None:
        return False
    if m["full_dd"] > max_dd_cap:
        return False
    if m["stress_dd"] > max_dd_cap:
        return False
    oos_pfs = [f["oos_pf"] for f in m["folds"] if f["oos_pf"] is not None]
    if not oos_pfs:
        return False
    if min(oos_pfs) < min_oos_pf:
        return False
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1199, help="History days (default 1199 = full cached data)")
    parser.add_argument("--full", action="store_true", help="Full sweep: more parameter combinations (slower)")
    args = parser.parse_args()
    cfg = load_config()
    print(f"Loading panel ({args.days} days)...")
    C, HI, LO, VQ, O, missing = load_panel(cfg, days=args.days, update=False)
    print(f"Loaded {len(C.columns)} symbols, {len(C)} days. Missing: {len(missing)}")

    fee = cfg.fee_taker
    slip = cfg.slippage
    cap = cfg.allocated_capital

    if args.full:
        lookbacks = list(range(5, 61, 5))   # 5,10,...,60
        breadths = [0.3, 0.4, 0.5, 0.6, 0.7]
        risks = [0.005, 0.01, 0.015, 0.02]
        weights = [0.05, 0.08, 0.10, 0.15, 0.20]
    else:
        lookbacks = [7, 8, 9, 10]
        breadths = [0.5]
        risks = [0.01]
        weights = [0.10]

    combos = list(itertools.product(lookbacks, breadths, risks, weights))
    print(f"Testing {len(combos)} combinations...\n")

    results = []
    for i, (lb, br, rp, mw) in enumerate(combos, 1):
        print(f"  [{i}/{len(combos)}] lookback={lb} breadth={br} risk={rp} max_wt={mw}", end=" ... ")
        m = d1_run(C, HI, LO, VQ, lb, br, rp, mw, fee, slip, cap)
        results.append(m)
        if m:
            print(f"PF={m['full_pf'] or 0:.2f} DD={m['full_dd']*100:.1f}% Eq={m['full_eq']:.0f}")
        else:
            print("FAILED")

    passing = [m for m in results if passes(m)]
    passing.sort(key=lambda m: (min(f["oos_pf"] for f in m["folds"] if f["oos_pf"] is not None) or 0), reverse=True)

    print("=== PASSED (OOS PF >= 1.0, DD < 20%, 2x-fee DD < 20%) ===")
    if not passing:
        print("NONE passed. Try relaxing caps or accept that no combo is robust on this data.")
    else:
        print(f"{'LB':>3} {'Br':>4} {'Risk':>6} {'MaxWt':>6} {'FullPF':>7} {'FullDD':>7} {'FullEq':>9} {'StressDD':>9} {'OOS_PF':>7} {'OOS_DD':>7} {'Trades':>7}")
        for m in passing[:10]:
            oos_pfs = [f["oos_pf"] for f in m["folds"] if f["oos_pf"] is not None]
            oos_dds = [f["oos_dd"] for f in m["folds"]]
            print(f"{m['lookback']:>3} {m['breadth_min']:>4} {m['risk_pct']:>6.3f} {m['max_weight']:>6.2f} "
                  f"{(m['full_pf'] or 0):>7.2f} {m['full_dd']*100:>6.1f}% {m['full_eq']:>9.2f} "
                  f"{m['stress_dd']*100:>8.1f}% {(min(oos_pfs) or 0):>7.2f} "
                  f"{max(oos_dds)*100:>6.1f}% {m['n_trades']:>7}")

    print("\n=== ALL RESULTS (sorted by OOS PF) ===")
    results_sorted = [m for m in results if m]
    results_sorted.sort(key=lambda m: (min(f["oos_pf"] for f in m["folds"] if f["oos_pf"] is not None) or -1), reverse=True)
    print(f"{'LB':>3} {'Br':>4} {'Risk':>6} {'MaxWt':>6} {'FullPF':>7} {'FullDD':>7} {'OOS_PF':>7} {'OOS_DD':>7} {'Pass':>5}")
    for m in results_sorted[:15]:
        oos_pfs = [f["oos_pf"] for f in m["folds"] if f["oos_pf"] is not None]
        oos_dds = [f["oos_dd"] for f in m["folds"]]
        ok = "YES" if passes(m) else "no"
        print(f"{m['lookback']:>3} {m['breadth_min']:>4} {m['risk_pct']:>6.3f} {m['max_weight']:>6.2f} "
              f"{(m['full_pf'] or 0):>7.2f} {m['full_dd']*100:>6.1f}% "
              f"{(min(oos_pfs) or 0):>7.2f} {max(oos_dds)*100:>6.1f}% {ok:>5}")

    print("\nNote: 'Pass' means OOS PF >= 1.0, full DD < 20%, 2x-fee DD < 20%.")
    print("Even passing combos are backtests — not a promise of live profit.")


if __name__ == "__main__":
    main()
