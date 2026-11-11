from __future__ import annotations

from decimal import Decimal

import okxspot.strategy.donchian as donchian
from okxspot.models import Position, Side
from okxspot.strategy.base import BarContext
from okxspot.strategy.donchian import DonchianAtrTrend
from tests.conftest import candle, day

D = Decimal


def _btc_up() -> tuple[object, ...]:
    prices = ["1", "2", "3", "4", "10", "11"]
    return tuple(candle(i, prices[i], inst="BTC-USDT") for i in range(6))


def _eth(close: str, high: str, low: str) -> tuple[object, ...]:
    bars = [candle(i, "95", inst="ETH-USDT", high="100", low="90", close="95") for i in range(5)]
    bars.append(candle(5, "100", inst="ETH-USDT", high=high, low=low, close=close))
    return tuple(bars)


def _ctx(history: tuple[object, ...], regime: tuple[object, ...], position: Position | None = None) -> BarContext:
    return BarContext("ETH-USDT", history, regime, position, history[-1].ts)  # type: ignore[arg-type]


def test_placeholder_is_marked() -> None:
    assert donchian.IS_PLACEHOLDER is True
    assert "PLACEHOLDER" in (donchian.__doc__ or "")


def test_breakout_uses_prior_channel_and_regime() -> None:
    strategy = DonchianAtrTrend(donchian_n=5, atr_n=3, atr_mult=D("2"), regime_sma=5)
    signals = strategy.on_bar(_ctx(_eth("105", "110", "99"), _btc_up()))
    assert len(signals) == 1
    assert signals[0].side is Side.BUY
    assert signals[0].stop_price is not None
    assert signals[0].stop_price < D("105")


def test_current_bar_high_alone_is_not_a_breakout() -> None:
    strategy = DonchianAtrTrend(donchian_n=5, atr_n=3, atr_mult=D("2"), regime_sma=5)
    # Close is not above the prior high of 100, even though this bar's high is 110.
    signals = strategy.on_bar(_ctx(_eth("100", "110", "94"), _btc_up()))
    assert signals == []


def test_regime_filter_blocks_entries() -> None:
    strategy = DonchianAtrTrend(donchian_n=5, atr_n=3, atr_mult=D("2"), regime_sma=5)
    falling = tuple(candle(i, "10", inst="BTC-USDT", close="1" if i == 5 else "10") for i in range(6))
    signals = strategy.on_bar(_ctx(_eth("105", "110", "99"), falling))
    assert signals == []


def test_exit_on_prior_low_break() -> None:
    strategy = DonchianAtrTrend(donchian_n=5, atr_n=3, atr_mult=D("2"), regime_sma=5)
    position = Position("ETH-USDT", D("1"), D("100"), D("90"), D("10"), day(0))
    history = list(_eth("95", "100", "90"))
    history[-1] = candle(5, "90", inst="ETH-USDT", open_="90", high="96", low="88", close="89")
    signals = strategy.on_bar(_ctx(tuple(history), _btc_up(), position))
    assert len(signals) == 1
    assert signals[0].side is Side.SELL
