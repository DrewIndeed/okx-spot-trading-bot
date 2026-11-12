"""Print the bot's current paper levels. No fixed take-profit.

Buy = my_d1 still in a 20-day breakout on the last closed 1Dutc bar.
SL  = ratchet mid-channel stop (only moves up).
Exit = close below that stop, or BTC close <= SMA200.
There is no take-profit price. Do not invent one.

Usage:
  .venv/Scripts/python scripts/signals_now.py
  .venv/Scripts/python scripts/signals_now.py --symbol HYPE
  .venv/Scripts/python scripts/signals_now.py --top 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingbot.cli import _feed, _load_d1_panel
from tradingbot.config import load_config
from tradingbot.strategy.my_d1 import MyD1Strategy
from tradingbot.universe import panel_from_candles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None, help="One coin, e.g. HYPE or HYPE-USDT")
    parser.add_argument("--top", type=int, default=10, help="How many nearest setups to show")
    args = parser.parse_args()

    cfg = load_config()
    feed = _feed(cfg)
    by, missing = _load_d1_panel(feed, cfg, days=800, update=False)
    C, HI, LO, VQ, _O = panel_from_candles(by)
    plugin = MyD1Strategy(
        cfg.allocated_capital,
        lookback=20,
        breadth_min=0.5,
        risk_pct=0.01,
        max_weight=0.10,
        exposure_scale=0.50,
    )
    plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
    state: dict = {}
    w, _trades = plugin.weights_and_trades(state=state)
    last = w.iloc[-1]
    held = last[last > 0].sort_values(ascending=False)

    btc = float(C["BTC"].iloc[-1])
    sma200 = float(C["BTC"].rolling(200).mean().iloc[-1])
    gate_on = btc > sma200
    print(f"as_of {C.index[-1].date()}  closed daily bar only")
    print(f"BTC {btc:.2f}  SMA200 {sma200:.2f}  gate {'ON' if gate_on else 'OFF'}")
    print(f"open breakouts: {len(held)}  gross weight {float(last.sum()):.3f}")
    print("TP: none. Exit = daily close under SL, or BTC gate off.")
    print()

    if held.empty:
        print("BUY NOW: none. Book is flat. No coin passed the 20-day breakout rules on this bar.")
    else:
        print(f"BUY / HOLD  top {min(args.top, len(held))}")
        print(f"{'coin':<8} {'weight':>8} {'entry':>12} {'SL':>12} {'last':>12} {'to_SL':>8}")
        for coin, wt in held.head(args.top).items():
            st = state.get(coin, {})
            entry = st.get("entry")
            stop = st.get("stop")
            px = float(C[coin].iloc[-1])
            dist = (px / stop - 1) if stop else float("nan")
            print(f"{coin:<8} {wt:>8.3f} {entry:>12.6g} {stop:>12.6g} {px:>12.6g} {dist:>7.1%}")

    # Nearest setups: distance from close to the breakout level (prior 20-day high).
    prev_high = C.shift(1).rolling(20).max()
    close = C.iloc[-1]
    level = prev_high.iloc[-1]
    gap = (level / close - 1).replace([float("inf"), float("-inf")], float("nan"))
    near = gap[(close.notna()) & (level.notna()) & (gap > 0)].sort_values()
    print()
    print(f"NOT buys. Closest to a breakout (need close > level, plus top-20 and breadth):")
    print(f"{'coin':<8} {'last':>12} {'breakout':>12} {'gap':>8}")
    for coin, g in near.head(args.top).items():
        print(f"{coin:<8} {float(close[coin]):>12.6g} {float(level[coin]):>12.6g} {g:>7.1%}")

    if args.symbol:
        raw = args.symbol.upper().replace("-USDT", "")
        print()
        print(f"--- {raw} ---")
        if raw not in C.columns:
            print("Not in the loaded universe.")
            return 1
        px = float(C[raw].iloc[-1])
        brk = float(prev_high[raw].iloc[-1])
        mid = float(((C[raw].rolling(20).max() + C[raw].rolling(20).min()) / 2).iloc[-1])
        in_book = raw in state
        print(f"last close {px:.6g}")
        print(f"breakout level (prior 20-day high) {brk:.6g}")
        print(f"would-be SL if a breakout fired today (20-day mid) {mid:.6g}")
        if in_book:
            st = state[raw]
            print(f"STATUS: IN BOOK  entry {st['entry']:.6g}  SL {st['stop']:.6g}  weight {float(last[raw]):.3f}")
            print("TP: none. Hold until close < SL or BTC gate off.")
        else:
            print("STATUS: NO ENTRY. Strategy does not want this coin on the last closed bar.")
            if px < brk:
                print(f"Reason: close is {brk / px - 1:.1%} under the breakout level.")
            else:
                print("Reason: breakout level was met, but top-20, breadth, or stop filter failed.")
        print("Do not force the SL as a live order. This is the paper rule, not a profit promise.")
    if missing:
        print(f"\nmissing cache: {len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
