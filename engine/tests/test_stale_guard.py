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
    # Form (b): last bar opened 02:00, closed 03:00; stale after 03:00 + 1H + 60s.
    guard = StaleGuard(bar="1H", tolerance_sec=60, max_clock_skew_ms=2000, max_price_move_pct=Decimal("0.05"))
    last_open = datetime(2026, 1, 2, 2, 0, tzinfo=UTC)
    previous = last_open - timedelta(hours=1)
    now = datetime(2026, 1, 2, 5, 0, 1, tzinfo=UTC)
    ok, reason = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=now,
        server_time_ms=int(now.timestamp() * 1000),
        last_price=Decimal("100"),
    )
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
    previous = _candle(datetime(2026, 1, 1, 23, 0, tzinfo=UTC), close="100")
    ok, reason = guard.check(
        candles=[previous, last], now=now, server_time_ms=int(now.timestamp() * 1000), last_price=Decimal("120")
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



# ----- Sofia stale-guard follow-up (form b) -----
# Interval is taken from the strategy timeframe (1H=3600, 4H=14400, 1Dutc=86400),
# not from config. tolerance_sec stays at the 900s default.
# Form (b): stale if now - last_close_ts > interval + tolerance
# where last_close_ts is the close of the last confirmed bar (open ts + interval).
# Form (a): stale if last_open_ts < floor(now/interval)*interval - tolerance
# is documented below; it is not implemented because it fails test #3.


def _default_guard(bar: str, **overrides: object) -> StaleGuard:
    kwargs: dict[str, object] = {
        "tolerance_sec": 900,
        "max_clock_skew_ms": 5000,
        "max_price_move_pct": Decimal("0.15"),
    }
    kwargs.update(overrides)
    return StaleGuard(bar=bar, **kwargs)  # type: ignore[arg-type]


def _form_a_open_ts_is_stale(last_open_ts: datetime, now: datetime, interval: int, tolerance: float) -> bool:
    """Sofia's form (a) applied to the OKX bar timestamp (period start).

    stale if last_close_ts < floor(now/interval)*interval - tolerance
    with last_close_ts taken as the candle open (OKX `ts`).
    """
    floor_now = int(now.timestamp() // interval * interval)
    return last_open_ts.timestamp() < floor_now - tolerance


def test_interval_comes_from_strategy_timeframe() -> None:
    assert _default_guard("1H").interval == 3600
    assert _default_guard("4H").interval == 14400
    assert _default_guard("1Dutc").interval == 86400
    assert _default_guard("1H").tolerance_sec == 900
    assert _default_guard("1Dutc").tolerance_sec == 900


def test_1h_mid_hour_is_not_stale() -> None:
    """1. 1H mid-hour is not stale under form (b) with default 900s tolerance."""
    guard = _default_guard("1H")
    last_open = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)  # closed at 10:00
    previous = last_open - timedelta(hours=1)
    now = datetime(2026, 1, 2, 10, 25, tzinfo=UTC)  # mid-hour
    ok, reason = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=now,
        server_time_ms=int(now.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok is True
    assert reason == "ok"


def test_missing_previous_bar_is_stale() -> None:
    """2. A missing previous bar is stale (gap or missing bar → stale)."""
    guard = _default_guard("1H")
    last_open = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    now = datetime(2026, 1, 2, 10, 10, tzinfo=UTC)
    ok, reason = guard.check(
        candles=[_candle(last_open)],
        now=now,
        server_time_ms=int(now.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok is False
    assert reason in {"candle_gap", "missing_bar", "stale_candle"}

    gapped = [_candle(last_open - timedelta(hours=3)), _candle(last_open)]
    ok_gap, reason_gap = guard.check(
        candles=gapped,
        now=now,
        server_time_ms=int(now.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok_gap is False
    assert reason_gap in {"candle_gap", "missing_bar", "stale_candle"}


def test_1d_just_after_midnight_utc_within_and_past_threshold() -> None:
    """3. 1D just after 00:00 UTC is not stale before the tolerance expires,
    and is stale after interval + tolerance.

    Form (b) uses last_close_ts = last confirmed open + interval (the close).
    Just after 00:00 the just-closed daily bar is only a few seconds old, so
    now - last_close_ts is well under interval + 900s. It becomes stale once
    that window has passed and no newer confirmed bar has arrived.
    """
    guard = _default_guard("1Dutc")
    assert guard.interval == 86400
    assert guard.tolerance_sec == 900
    last_open = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    previous = last_open - timedelta(days=1)
    last_close = last_open + timedelta(seconds=guard.interval)  # 2026-01-02 00:00
    just_after = last_close + timedelta(seconds=30)
    ok, reason = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=just_after,
        server_time_ms=int(just_after.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok is True
    assert reason == "ok"

    # Still inside the tolerance after the close (tolerance has not expired).
    before_tol = last_close + timedelta(seconds=899)
    ok_tol, reason_tol = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=before_tol,
        server_time_ms=int(before_tol.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok_tol is True
    assert reason_tol == "ok"

    # Mid-window on 2 Jan: form (b) is still fresh, but measuring age from the
    # bar open (last.ts) would already be stale (36h > interval + 900s).
    mid_day = last_close + timedelta(hours=12)
    ok_mid, reason_mid = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=mid_day,
        server_time_ms=int(mid_day.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok_mid is True
    assert reason_mid == "ok"

    past = last_close + timedelta(seconds=int(guard.interval + guard.tolerance_sec + 1))
    ok_late, reason_late = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=past,
        server_time_ms=int(past.timestamp() * 1000),
        last_price=Decimal("100"),
    )
    assert ok_late is False
    assert reason_late == "stale_candle"


def test_form_a_fails_just_after_1d_boundary() -> None:
    """Documented case: form (a) fails Sofia's test #3, which is why (b) was chosen.

    Form (a): stale if last_close_ts < floor(now/interval)*interval - tolerance.

    If last_close_ts is taken as the OKX candle timestamp (the bar open, which
    is what the API provides), then at 00:00:30 UTC on 2 Jan the last confirmed
    1D bar is still yesterday's (open 1 Jan 00:00). floor(now/86400)*86400 is
    2 Jan 00:00, minus 900s is 1 Jan 23:45, and 1 Jan 00:00 is already less
    than that. Form (a) therefore marks the bar stale immediately after the
    daily boundary, before OKX has confirmed the new day's bar.

    Form (b) does not: just after 00:00, now - last_close_ts is ~30s, which
    is not greater than interval + tolerance.
    """
    interval = 86400
    tolerance = 900.0
    last_open = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    just_after = datetime(2026, 1, 2, 0, 0, 30, tzinfo=UTC)
    assert _form_a_open_ts_is_stale(last_open, just_after, interval, tolerance) is True

    # And form (b) with last_close_ts = open + interval stays fresh, matching test #3.
    last_close = last_open + timedelta(seconds=interval)
    age = (just_after - last_close).total_seconds()
    assert not (age > interval + tolerance)
    assert age <= interval + tolerance


def test_sofia_clock_skew_still_blocks() -> None:
    """4. Clock skew still blocks, even when the 1H bar itself is mid-hour fresh."""
    guard = _default_guard("1H")
    last_open = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    previous = last_open - timedelta(hours=1)
    now = datetime(2026, 1, 2, 10, 25, tzinfo=UTC)
    server = int(now.timestamp() * 1000) + 20_000
    ok, reason = guard.check(
        candles=[_candle(previous), _candle(last_open)],
        now=now,
        server_time_ms=server,
        last_price=Decimal("100"),
    )
    assert ok is False
    assert reason == "clock_skew"
