"""PLACEHOLDER trend strategy — pending a researched specification.

This is not a claim of edge or profitability. Earlier reversal research failed.
Do not enable live trading on this model until a specification is accepted and
walk-forward results are reviewed by the owner.
"""

from __future__ import annotations

from decimal import Decimal

from okxspot.models import Side, Signal
from okxspot.strategy.base import BarContext
from okxspot.strategy.indicators import average_true_range, prior_donchian, sma

IS_PLACEHOLDER = True


class DonchianAtrTrend:
    """Long-only Donchian breakout with an ATR stop and a BTC SMA regime filter.

    PLACEHOLDER. Rules, pending a real specification:

    * Regime: latest BTC close is strictly above its SMA.
    * Entry: close breaks the prior N-bar Donchian high. The current bar is
      not part of the channel.
    * Stop: close minus ATR times a multiplier.
    * Exit: close breaks the prior N-bar Donchian low.
    """

    name = "donchian_atr"

    def __init__(
        self,
        *,
        donchian_n: int = 20,
        atr_n: int = 14,
        atr_mult: Decimal = Decimal("2"),
        regime_sma: int = 200,
    ) -> None:
        self.donchian_n = donchian_n
        self.atr_n = atr_n
        self.atr_mult = atr_mult
        self.regime_sma = regime_sma

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        channel = prior_donchian(list(ctx.history), self.donchian_n)
        if channel is None:
            return []
        upper, lower = channel
        close = ctx.history[-1].close
        if ctx.position is not None and close < lower:
            return [Signal(ctx.inst_id, Side.SELL, None, "donchian_exit_placeholder")]
        if ctx.position is not None:
            return []
        if not self._regime_on(ctx):
            return []
        if close <= upper:
            return []
        atr = average_true_range(list(ctx.history), self.atr_n)
        if atr is None or atr <= 0:
            return []
        stop = close - atr * self.atr_mult
        if stop <= 0 or stop >= close:
            return []
        return [Signal(ctx.inst_id, Side.BUY, stop, "donchian_breakout_placeholder")]

    def _regime_on(self, ctx: BarContext) -> bool:
        closes = [bar.close for bar in ctx.regime_history]
        average = sma(closes, self.regime_sma)
        if average is None or not ctx.regime_history:
            return False
        return ctx.regime_history[-1].close > average
