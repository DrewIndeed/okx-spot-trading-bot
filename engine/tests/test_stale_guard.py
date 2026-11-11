from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxspot.models import Candle
from okxspot.stale import StaleDataError, StaleGuard


def _candle(ts: datetime, close: str = "100", inst: str = "BTC-USDT") -> Candle:
    px = Decimal(close)
    return Candle(inst, ts, px, px, px, px, Decimal("1"), True)


def test_refuses_stale_closed_candle() -> None:
    guard = StaleGuard(bar="1H", tolerance_sec=60, max_clock_skew_ms=2000, max_price_move_pct=Decimal("0.05"))
    now = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
    last = _candle(datetime(2026, 1, 2, 2, 0, tzinfo=UTC))  # 2h old, interval 1h + 60s
    ok, reason = guard.check(candles=[last], now=now, server_time_ms=int(now.timestamp() * 1000), last_price=Decimal("100"))
    assert ok is False
    assert reason == "stale_candle"


def test_refuses_candle_gap() -> None:
    guard = StaleGuard(bar="1H", tolerance_sec=60, max_clock_skew_ms=2000, max_price_move_pct=Decimal("0.05"), max_gap_bars=1)
    t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    candles = [_candle(t0), _candle(t0 + timedelta(hours=3))]
    now = t0 + timedelta(hours=4)
    ok, reason = guard.check(
        candles=candles, now=now, server_time_ms=int(now.timestamp() * 1000), last_price=Decimal("100")
    )
    assert ok is False
    assert reason == "candle_gap"


def test_refuses_clock_skew() -> None:
    guard = StaleGuard(bar="1H", tolerance_sec=300, max_clock_skew_ms=2000, max_price_move_pct=Decimal("0.50"))
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    last = _candle(datetime(2026, 1, 2, 0, 0, tzinfo=UTC))
    server = int(now.timestamp() * 1000) + 10_000
    ok, reason = guard.check(candles=[last], now=now, server_time_ms=server, last_price=Decimal("100"))
    assert ok is False
    assert reason == "clock_skew"


def test_refuses_price_jump_versus_last_bar() -> None:
    guard = StaleGuard(bar="1H", tolerance_sec=300, max_clock_skew_ms=20_000, max_price_move_pct=Decimal("0.05"))
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    last = _candle(datetime(2026, 1, 2, 0, 0, tzinfo=UTC), close="100")
    ok, reason = guard.check(
        candles=[last], now=now, server_time_ms=int(now.timestamp() * 1000), last_price=Decimal("120")
    )
    assert ok is False
    assert reason == "price_jump"


def test_fresh_data_is_allowed() -> None:
    guard = StaleGuard(bar="1H", tolerance_sec=300, max_clock_skew_ms=5000, max_price_move_pct=Decimal("0.05"))
    t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    candles = [_candle(t0), _candle(t0 + timedelta(hours=1), close="101")]
    now = t0 + timedelta(hours=1, minutes=2)
    ok, reason = guard.check(
        candles=candles, now=now, server_time_ms=int(now.timestamp() * 1000), last_price=Decimal("101.5")
    )
    assert ok is True
    assert reason == "ok"


def test_assert_tradeable_raises() -> None:
    guard = StaleGuard(bar="1D", tolerance_sec=60, max_clock_skew_ms=1000, max_price_move_pct=Decimal("0.01"))
    now = datetime(2026, 1, 10, tzinfo=UTC)
    with pytest.raises(StaleDataError, match="stale_candle"):
        guard.assert_tradeable(
            candles=[_candle(datetime(2026, 1, 1, tzinfo=UTC))],
            now=now,
            server_time_ms=int(now.timestamp() * 1000),
            last_price=Decimal("100"),
        )

