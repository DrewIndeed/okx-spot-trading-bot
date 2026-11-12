"""Max profit under a hard drawdown cap. Train scale, score holdout.

Cash (scale=0) is always a candidate. If every positive size fails the cap
or loses money out of sample, the optimum is do not trade.

Not a live-trading recommendation. Does not modify locked strategy files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingbot.config import load_config
from tradingbot.data.rest import OkxRestFeed
from tradingbot.regime import btc_above_sma200
from tradingbot.universe import (
    breadth_top20,
    inst_id,
    load_favorites,
    monthly_top20_universe,
    panel_from_candles,
)

DD_CAP = 0.20
TRAIN_FRAC = 0.70


def load_panel(cfg, days: int = 1199):
    feed = OkxRestFeed(
        cache_dir=cfg.data.cache_dir,
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )
    by = {}
    for b in load_favorites(cfg.universe_file):
        inst = inst_id(b)
        cs = feed.get_candles(inst, "1Dutc", days=days, update=False, closed_only=True)
        if cs:
            by[b] = cs
    if "BTC" not in by:
        raise SystemExit("BTC 1Dutc cache missing. Run data-sync first.")
    C, HI, LO, VQ, _O = panel_from_candles(by)
    return C, VQ


def weight_path(C: pd.DataFrame, VQ: pd.DataFrame, lookback: int, breadth_min: float, risk_pct: float, max_weight: float) -> pd.DataFrame:
    U = monthly_top20_universe(C, quote_vol=VQ, top_n=20)
    br = breadth_top20(C, U).to_numpy()
    gate = btc_above_sma200(C["BTC"]).reindex(C.index).fillna(0.0).to_numpy()
    w = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for s in C.columns:
        c = C[s].to_numpy()
        u = U[s].to_numpy()
        prevmax = C[s].shift(1).rolling(lookback).max().to_numpy()
        mx = C[s].rolling(lookback).max().to_numpy()
        mn = C[s].rolling(lookback).min().to_numpy()
        pos = False
        st = ep = risk = size = 0.0
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
            elif (
                u[t]
                and gate[t] > 0
                and br[t] >= breadth_min
                and not np.isnan(prevmax[t])
                and c[t] > prevmax[t]
                and not np.isnan(stop_t)
                and stop_t < c[t]
            ):
                pos = True
                ep = float(c[t])
                st = float(stop_t)
                risk = (ep - st) / ep if ep else 0.0
                size = min(risk_pct / risk, max_weight) if risk > 0 else 0.0
            col[t] = size if pos else 0.0
        w[s] = col
    gross = w.sum(axis=1).to_numpy()
    w = w.div(np.maximum(gross, 1.0), axis=0)
    w = w.mul(pd.Series(gate > 0, index=C.index), axis=0)
    return w


def daily_components(C: pd.DataFrame, w: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    ret = (C / C.shift(1) - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    prev = w.shift(1).fillna(0.0)
    gross = (prev * ret).sum(axis=1).to_numpy()
    turn = (w - prev).abs().sum(axis=1).to_numpy()
    return gross, turn


def path_stats(gross: np.ndarray, turn: np.ndarray, cost: float, scale: float, cap: float) -> dict:
    eq = cap
    peak = cap
    max_dd = 0.0
    for i in range(1, len(gross)):
        net = scale * (gross[i] - cost * turn[i])
        eq *= 1.0 + net
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return {"end": eq, "dd": max_dd, "pnl": eq - cap}


def hold_stats(px: pd.Series, cost: float, cap: float) -> dict:
    r = px.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    eq = cap * (1.0 - cost)  # one entry
    peak = eq
    max_dd = 0.0
    for i in range(1, len(r)):
        eq *= 1.0 + r[i]
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    eq *= 1.0 - cost  # one exit
    return {"end": eq, "dd": max_dd, "pnl": eq - cap}


def best_scale(gross, turn, fee, slip, cap, dd_cap: float) -> tuple[float, dict, dict]:
    best_k = 0.0
    best = path_stats(gross, turn, fee + slip, 0.0, cap)
    best_stress = path_stats(gross, turn, 2 * fee + 2 * slip, 0.0, cap)
    for k in np.round(np.arange(0.0, 1.01, 0.05), 2):
        base = path_stats(gross, turn, fee + slip, float(k), cap)
        stress = path_stats(gross, turn, 2 * fee + 2 * slip, float(k), cap)
        if base["dd"] > dd_cap or stress["dd"] > dd_cap:
            continue
        if base["end"] > best["end"]:
            best_k, best, best_stress = float(k), base, stress
    return best_k, best, best_stress


def main() -> int:
    cfg = load_config()
    print("load panel...")
    C, VQ = load_panel(cfg)
    print(f"symbols={len(C.columns)} days={len(C)} {C.index[0].date()} -> {C.index[-1].date()}")
    fee, slip, cap = cfg.fee_taker, cfg.slippage, cfg.allocated_capital
    split = int(len(C) * TRAIN_FRAC)

    print("\nBENCH (full window, one entry + one exit cost)")
    btc = hold_stats(C["BTC"], fee + slip, cap)
    if "ETH" in C.columns:
        mix = 0.5 * C["BTC"].pct_change().fillna(0) + 0.5 * C["ETH"].pct_change().fillna(0)
        # reuse hold_stats via a synthetic price
        synth = (1 + mix).cumprod() * float(C["BTC"].iloc[0])
        bench = hold_stats(synth, fee + slip, cap)
    else:
        bench = None
    print(f"  BTC hold     end={btc['end']:.0f}  DD={btc['dd']:.1%}  pnl={btc['pnl']:.0f}")
    if bench:
        print(f"  50/50 BT+ETH end={bench['end']:.0f}  DD={bench['dd']:.1%}  pnl={bench['pnl']:.0f}")
    print(f"  CASH         end={cap:.0f}  DD=0.0%  pnl=0")

    paths = {}
    for lb in (7, 10, 20):
        print(f"weights lookback={lb} ...", flush=True)
        w = weight_path(C, VQ, lb, 0.5, 0.01, 0.10)
        paths[lb] = daily_components(C, w)

    for dd_cap in (0.20, 0.25, 0.30, 0.35):
        print(f"\nCAP {dd_cap:.0%}  train picks scale, holdout is the score")
        print(f"{'LB':>4} {'k_train':>8} {'train_end':>10} {'train_dd':>9} {'OOS_end':>10} {'OOS_dd':>8} {'OOS_2xDD':>9} {'beat_cash':>10}")
        for lb, (gross, turn) in paths.items():
            k, train, _stress = best_scale(gross[: split + 1], turn[: split + 1], fee, slip, cap, dd_cap)
            oos = path_stats(gross[split:], turn[split:], fee + slip, k, cap)
            oos_2x = path_stats(gross[split:], turn[split:], 2 * fee + 2 * slip, k, cap)
            beat = oos["end"] > cap and oos["dd"] <= dd_cap and oos_2x["dd"] <= dd_cap
            print(
                f"{lb:>4} {k:>8.2f} {train['end']:>10.0f} {train['dd']:>8.1%} "
                f"{oos['end']:>10.0f} {oos['dd']:>7.1%} {oos_2x['dd']:>8.1%} {str(beat):>10}"
            )
    print("\nPick from train column only. Holdout must still beat cash before the bot uses a higher scale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
