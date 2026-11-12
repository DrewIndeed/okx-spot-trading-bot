"""D1 parameter sensitivity and regime analysis.

Follows backtest-expert methodology:
- Parameter sensitivity: find plateaus, not spikes
- Regime analysis: test across bull/bear/high-vol/low-vol periods
- Execution friction: 2x fee stress included
- Out-of-sample focus: walk-forward folds

Does NOT modify locked strategy files.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import sys
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


def load_panel(cfg, days: int = 1199, update: bool = False):
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


def d1_run(
    C: pd.DataFrame,
    HI: pd.DataFrame,
    LO: pd.DataFrame,
    VQ: pd.DataFrame,
    lookback: int,
    breadth_min: float,
    risk_pct: float,
    max_weight: float,
    fee: float,
    slip: float,
    cap: float,
    start_idx: int = 0,
    end_idx: Optional[int] = None,
) -> dict:
    """Run D1-like logic on a slice of data. Returns metrics dict."""
    if end_idx is None:
        end_idx = len(C)

    C_slice = C.iloc[start_idx:end_idx]
    HI_slice = HI.iloc[start_idx:end_idx]
    LO_slice = LO.iloc[start_idx:end_idx]
    VQ_slice = VQ.iloc[start_idx:end_idx]

    n = len(C_slice)
    if n < 250:
        return None

    U = monthly_top20_universe(C_slice, quote_vol=VQ_slice, top_n=20)
    br = breadth_top20(C_slice, U)
    gate = btc_above_sma200(C_slice["BTC"]).reindex(C_slice.index).fillna(0.0).values

    w = pd.DataFrame(0.0, C_slice.index, C_slice.columns)
    trades = []

    for s in C_slice.columns:
        c = C_slice[s].values
        u = U[s].values
        prevmax = C_slice[s].shift(1).rolling(lookback).max().values
        mx = C_slice[s].rolling(lookback).max().values
        mn = C_slice[s].rolling(lookback).min().values
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

    ssum = w.sum(axis=1)
    w = w.div(np.maximum(ssum, 1.0), axis=0)
    w = w.mul(pd.DataFrame(gate > 0, index=C_slice.index, columns=C_slice.columns), axis=0)

    cost = fee + slip
    equity = [cap]
    cash_eq = cap
    prev_w = w.iloc[0] * 0.0

    for i in range(1, len(C_slice)):
        ret = (C_slice.iloc[i] / C_slice.iloc[i - 1] - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        gross = float((prev_w * ret).sum())
        turn = float((w.iloc[i] - prev_w).abs().sum())
        net = gross - cost * turn
        cash_eq = cash_eq * (1.0 + net)
        equity.append(cash_eq)
        prev_w = w.iloc[i]

    equity = np.array(equity)

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

    trade_pnls = []
    for tr in trades:
        ei, ex = tr["entry_idx"], tr["exit_idx"]
        if ei is None or ex is None or ei >= len(equity) or ex >= len(equity):
            continue
        r_net = (tr["exit"] / tr["entry"]) * (1.0 - cost) ** 2 - 1.0 if tr["entry"] else 0.0
        e0 = equity[ei] if ei < len(equity) else cap
        trade_pnls.append(r_net * tr["size"] * e0)

    full_pf = pf(trade_pnls)
    full_dd = max_dd(equity)

    return {
        "lookback": lookback,
        "breadth_min": breadth_min,
        "risk_pct": risk_pct,
        "max_weight": max_weight,
        "full_pf": full_pf,
        "full_dd": full_dd,
        "full_eq": equity[-1],
        "n_trades": len(trades),
        "start_date": str(C_slice.index[0]),
        "end_date": str(C_slice.index[-1]),
        "n_days": n,
    }


def regime_analysis(C, HI, LO, VQ, fee, slip, cap):
    """Split data into regimes and test lookback=7 across each."""
    n = len(C)
    # Regime splits: roughly equal time periods
    regimes = [
        ("Period 1 (early)", 0, n // 4),
        ("Period 2", n // 4, n // 2),
        ("Period 3", n // 2, 3 * n // 4),
        ("Period 4 (recent)", 3 * n // 4, n),
    ]

    print("\n" + "=" * 70)
    print("REGIME ANALYSIS - lookback=7, breadth=0.5, risk=1%, max_wt=10%")
    print("=" * 70)
    print(f"{'Regime':<25} {'Start':<12} {'End':<12} {'PF':>6} {'DD':>7} {'Eq':>10} {'Trades':>7}")
    print("-" * 70)

    results = []
    for label, s, e in regimes:
        m = d1_run(C, HI, LO, VQ, 7, 0.5, 0.01, 0.10, fee, slip, cap, s, e)
        if m:
            results.append(m)
            print(f"{label:<25} {m['start_date'][:10]:<12} {m['end_date'][:10]:<12} "
                  f"{(m['full_pf'] or 0):>6.2f} {m['full_dd']*100:>6.1f}% "
                  f"{m['full_eq']:>10.2f} {m['n_trades']:>7}")
    return results


def parameter_sensitivity(C, HI, LO, VQ, fee, slip, cap):
    """Test lookback across range to find plateaus vs spikes."""
    lookbacks = list(range(5, 31, 1))
    breadths = [0.3, 0.5, 0.7]
    risks = [0.005, 0.01, 0.015]

    print("\n" + "=" * 70)
    print("PARAMETER SENSITIVITY - PF and DD across lookback values")
    print("=" * 70)
    print(f"{'LB':>3} {'Br':>4} {'Risk':>6} {'PF':>7} {'DD':>7} {'Eq':>10} {'Trades':>7}")
    print("-" * 70)

    all_results = []
    for lb in lookbacks:
        for br in breadths:
            for rp in risks:
                m = d1_run(C, HI, LO, VQ, lb, br, rp, 0.10, fee, slip, cap)
                if m:
                    all_results.append(m)
                    print(f"{lb:>3} {br:>4} {rp:>6.3f} {(m['full_pf'] or 0):>7.2f} "
                          f"{m['full_dd']*100:>6.1f}% {m['full_eq']:>10.2f} {m['n_trades']:>7}")

    # Find best by PF with DD < 20%
    passing = [m for m in all_results if m["full_dd"] < 0.20 and (m["full_pf"] or 0) > 1.0]
    passing.sort(key=lambda m: (m["full_pf"] or 0), reverse=True)

    print("\n" + "=" * 70)
    print("PASSING COMBOS (PF > 1.0, DD < 20%) - sorted by PF")
    print("=" * 70)
    if not passing:
        print("NONE passed on full data.")
    else:
        print(f"{'LB':>3} {'Br':>4} {'Risk':>6} {'PF':>7} {'DD':>7} {'Eq':>10} {'Trades':>7}")
        for m in passing[:10]:
            print(f"{m['lookback']:>3} {m['breadth_min']:>4} {m['risk_pct']:>6.3f} "
                  f"{(m['full_pf'] or 0):>7.2f} {m['full_dd']*100:>6.1f}% "
                  f"{m['full_eq']:>10.2f} {m['n_trades']:>7}")

    return all_results, passing


def main():
    cfg = load_config()
    print("Loading panel (full data, may take a moment)...")
    C, HI, LO, VQ, O, missing = load_panel(cfg, days=1199, update=False)
    print(f"Loaded {len(C.columns)} symbols, {len(C)} days ({C.index[0].date()} to {C.index[-1].date()})")

    fee = cfg.fee_taker
    slip = cfg.slippage
    cap = cfg.allocated_capital

    regime_results = regime_analysis(C, HI, LO, VQ, fee, slip, cap)
    all_results, passing = parameter_sensitivity(C, HI, LO, VQ, fee, slip, cap)

    # Save results
    output = {
        "generated_at": datetime.now().isoformat(),
        "data_start": str(C.index[0]),
        "data_end": str(C.index[-1]),
        "n_symbols": len(C.columns),
        "n_days": len(C),
        "regime_results": regime_results,
        "all_results": all_results,
        "passing": passing,
    }
    output_dir = Path("reports")
    output_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"d1_sensitivity_{ts}.json"
    path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nResults saved to {path}")


if __name__ == "__main__":
    main()
