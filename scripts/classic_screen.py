"""Pre-registered classic rules on cached OKX 1Dutc. Not a parameter mine.

Rules fixed before the run. Train = first 70%. Holdout = last 30%.
Winner, if any, is the train book with the highest end equity among books
that also keep train max-DD <= 20% and 2x-cost train max-DD <= 20%.
Holdout is reported, not used to pick.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
COST = 0.001 + 0.002  # fee + slippage, per side, matches config.yaml
CAP = 10_000.0
DD_CAP = 0.20
TRAIN_FRAC = 0.70


def load_close(inst: str) -> pd.Series:
    p = CACHE / f"{inst}_1Dutc.parquet"
    df = pd.read_parquet(p)
    idx = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    s = pd.Series(df["close"].astype(float).to_numpy(), index=idx, name=inst)
    return s.sort_index()


def max_dd(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.maximum(peak, 1e-12)
    return float(dd.max()) if len(dd) else 0.0


def simulate(pos: pd.Series, ret: pd.Series, cost: float = COST, cap: float = CAP) -> dict:
    """pos[t] is the weight held over the return from t to t+1. Causal: shift inside."""
    w = pos.fillna(0.0).clip(lower=0.0)
    r = ret.fillna(0.0)
    held = w.shift(1).fillna(0.0)
    turn = (w - held).abs()
    # charge cost when the weight changes, at that bar, before the next return
    net = held * r - cost * turn
    eq = cap * (1.0 + net).cumprod()
    eq_v = eq.to_numpy()
    return {
        "end": float(eq_v[-1]) if len(eq_v) else cap,
        "dd": max_dd(eq_v) if len(eq_v) else 0.0,
        "n": int((held > 0).sum()),
    }


def sma_pos(close: pd.Series, n: int) -> pd.Series:
    sma = close.rolling(n, min_periods=n).mean()
    return (close > sma).astype(float)


def donchian_pos(close: pd.Series, entry_n: int, exit_n: int) -> pd.Series:
    prev_high = close.shift(1).rolling(entry_n, min_periods=entry_n).max()
    prev_low = close.shift(1).rolling(exit_n, min_periods=exit_n).min()
    pos = np.zeros(len(close))
    in_pos = False
    c = close.to_numpy()
    hi = prev_high.to_numpy()
    lo = prev_low.to_numpy()
    for i in range(len(c)):
        if np.isnan(c[i]) or np.isnan(hi[i]) or np.isnan(lo[i]):
            in_pos = False
            continue
        if in_pos and c[i] < lo[i]:
            in_pos = False
        elif (not in_pos) and c[i] > hi[i]:
            in_pos = True
        pos[i] = 1.0 if in_pos else 0.0
    return pd.Series(pos, index=close.index)


def vol_target_pos(close: pd.Series, target_ann: float, lookback: int = 20, gate: pd.Series | None = None) -> pd.Series:
    r = close.pct_change()
    vol = r.rolling(lookback, min_periods=lookback).std() * np.sqrt(365)
    raw = (target_ann / vol).clip(upper=1.0)
    if gate is not None:
        raw = raw.where(gate > 0, 0.0)
    return raw.fillna(0.0)


def slice_stats(close: pd.Series, pos: pd.Series, start: int, end: int, cost: float = COST) -> dict:
    c = close.iloc[start:end]
    p = pos.reindex(c.index).fillna(0.0)
    return simulate(p, c.pct_change(), cost=cost)


def main() -> int:
    btc = load_close("BTC-USDT")
    eth = load_close("ETH-USDT")
    idx = btc.index.intersection(eth.index)
    btc = btc.reindex(idx)
    eth = eth.reindex(idx)
    n = len(btc)
    split = int(n * TRAIN_FRAC)
    print(f"BTC/ETH {btc.index[0].date()} -> {btc.index[-1].date()}  n={n}  split={btc.index[split].date()}")

    books: dict[str, pd.Series] = {
        "cash": pd.Series(0.0, index=idx),
        "btc_hold": pd.Series(1.0, index=idx),
        "btc_sma50": sma_pos(btc, 50),
        "btc_sma100": sma_pos(btc, 100),
        "btc_sma200": sma_pos(btc, 200),
        "btc_donchian_20_10": donchian_pos(btc, 20, 10),
        "btc_donchian_55_20": donchian_pos(btc, 55, 20),
        "btc_vol15": vol_target_pos(btc, 0.15),
        "btc_vol25": vol_target_pos(btc, 0.25),
        "btc_sma200_vol25": vol_target_pos(btc, 0.25, gate=sma_pos(btc, 200)),
        "eth_sma200": sma_pos(eth, 200),
    }

    print(f"\n{'book':<22} {'train_end':>10} {'train_dd':>9} {'oos_end':>10} {'oos_dd':>8} {'oos_2x_dd':>10}")
    rows = []
    for name, pos in books.items():
        # align pos to the asset it trades
        asset = eth if name.startswith("eth") else btc
        tr = slice_stats(asset, pos, 0, split)
        oos = slice_stats(asset, pos, split, n)
        oos2 = slice_stats(asset, pos, split, n, cost=COST * 2)
        rows.append((name, tr, oos, oos2))
        print(f"{name:<22} {tr['end']:>10.0f} {tr['dd']:>8.1%} {oos['end']:>10.0f} {oos['dd']:>7.1%} {oos2['dd']:>9.1%}")

    eligible = [
        r for r in rows
        if r[1]["dd"] <= DD_CAP and slice_stats(
            eth if r[0].startswith("eth") else btc, books[r[0]], 0, split, cost=COST * 2
        )["dd"] <= DD_CAP
    ]
    eligible.sort(key=lambda r: r[1]["end"], reverse=True)
    print("\nTRAIN PICK under 20% DD, base and 2x cost:")
    if not eligible:
        print("  none. Cash wins the constraint.")
    else:
        name, tr, oos, oos2 = eligible[0]
        beat = oos["end"] > CAP and oos["dd"] <= DD_CAP and oos2["dd"] <= DD_CAP
        print(f"  {name}  train_end={tr['end']:.0f}  oos_end={oos['end']:.0f}  oos_dd={oos['dd']:.1%}  holdout_ok={beat}")

    print("\nUnconstrained train leader (ignore DD cap):")
    leader = max(rows, key=lambda r: r[1]["end"])
    print(f"  {leader[0]}  train_end={leader[1]['end']:.0f}  train_dd={leader[1]['dd']:.1%}  oos_end={leader[2]['end']:.0f}  oos_dd={leader[2]['dd']:.1%}")
    print("Huge holdout profit with DD<=20% is the question. Read the table. Do not pick from the holdout column.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
