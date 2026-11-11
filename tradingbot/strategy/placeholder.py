"""PLACEHOLDER strategy — EMA cross + ATR stop. NOT a proven edge. Do not treat results as profitability.

Swap this out when Cypheous's strategy spec arrives (implement tradingbot.strategy.base.Strategy).
Spot-only: long on fast EMA crossing above slow EMA; exit on death cross or ATR stop. No shorts.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from tradingbot.indicators import atr, ema
from tradingbot.types import Candle, Position, Signal

# Clearly labeled so reports and logs cannot be mistaken for a researched edge.
PLACEHOLDER_NAME = "PLACEHOLDER_ema_cross_atr"
PLACEHOLDER_DISCLAIMER = (
    "PLACEHOLDER strategy (EMA cross + ATR stop). Not a proven edge. "
    "Results are not a claim of profitability."
)


class PlaceholderEmaAtrStrategy:
    name = PLACEHOLDER_NAME

    def __init__(self, fast: int = 12, slow: int = 26, atr_n: int = 14, atr_mult: float = 2.0):
        if fast >= slow:
            raise ValueError("fast EMA period must be < slow")
        self.fast = fast
        self.slow = slow
        self.atr_n = atr_n
        self.atr_mult = atr_mult

    def warmup_bars(self) -> int:
        return max(self.slow, self.atr_n) + 2

    def on_candles(
        self,
        inst_id: str,
        candles: Sequence[Candle],
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        n = len(candles)
        if n < self.warmup_bars():
            return None
        # last bar must be confirmed (caller should already filter, but be safe)
        if candles[-1].confirm == 0:
            candles = [c for c in candles if c.confirm == 1]
            if len(candles) < self.warmup_bars():
                return None
        close = np.array([c.close for c in candles], float)
        high = np.array([c.high for c in candles], float)
        low = np.array([c.low for c in candles], float)
        fast = ema(close, self.fast)
        slow = ema(close, self.slow)
        a = atr(high, low, close, self.atr_n)
        i = len(close) - 1
        if i < 1 or np.isnan(fast[i]) or np.isnan(slow[i]) or np.isnan(fast[i - 1]) or np.isnan(slow[i - 1]):
            return None
        if np.isnan(a[i]) or a[i] <= 0:
            return None
        ts = int(candles[-1].ts)
        bullish_cross = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        bearish_cross = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        in_pos = position is not None and position.qty > 0

        if in_pos and bearish_cross:
            return Signal(
                inst_id=inst_id,
                side="close",
                ts=ts,
                signal_id=f"{self.name}:{inst_id}:{ts}:close",
                reason="placeholder death-cross exit",
                strategy=self.name,
            )
        if (not in_pos) and bullish_cross:
            stop = float(close[i] - self.atr_mult * a[i])
            if stop >= close[i]:
                return None
            return Signal(
                inst_id=inst_id,
                side="buy",
                ts=ts,
                signal_id=f"{self.name}:{inst_id}:{ts}:buy",
                stop_price=stop,
                reason=f"placeholder EMA({self.fast}) cross above EMA({self.slow}); ATR stop {self.atr_mult}x",
                strategy=self.name,
            )
        return None
