from __future__ import annotations

from decimal import Decimal

from okxspot.models import Candle


def sma(values: list[Decimal], period: int) -> Decimal | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window, start=Decimal("0")) / Decimal(period)


def average_true_range(candles: list[Candle], period: int) -> Decimal | None:
    """Wilder ATR. Needs ``period + 1`` candles. Uses only the supplied series."""
    if period <= 0 or len(candles) < period + 1:
        return None
    true_ranges: list[Decimal] = []
    for index in range(1, len(candles)):
        prev = candles[index - 1]
        current = candles[index]
        true_range = max(
            current.high - current.low,
            abs(current.high - prev.close),
            abs(current.low - prev.close),
        )
        true_ranges.append(true_range)
    atr = sum(true_ranges[:period], start=Decimal("0")) / Decimal(period)
    for true_range in true_ranges[period:]:
        atr = (atr * Decimal(period - 1) + true_range) / Decimal(period)
    return atr


def prior_donchian(candles: list[Candle], period: int) -> tuple[Decimal, Decimal] | None:
    """High and low of the prior ``period`` bars, excluding the current bar."""
    if period <= 0 or len(candles) < period + 1:
        return None
    window = candles[-(period + 1) : -1]
    if len(window) != period:
        return None
    return max(bar.high for bar in window), min(bar.low for bar in window)
