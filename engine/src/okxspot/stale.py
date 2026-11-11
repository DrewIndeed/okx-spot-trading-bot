from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from okxspot.errors import OkxSpotError
from okxspot.models import Candle

_BAR_SECONDS = {
    "1M": 60,
    "1m": 60,
    "5M": 300,
    "5m": 300,
    "15M": 900,
    "15m": 900,
    "1H": 3600,
    "1h": 3600,
    "4H": 14400,
    "4h": 14400,
    "1D": 86400,
    "1d": 86400,
    "1Dutc": 86400,
    "1DUTC": 86400,
}


class StaleDataError(OkxSpotError):
    """Market data is too old, gapped, skewed, or inconsistent to trade."""


def bar_interval_seconds(bar: str) -> int:
    if bar in _BAR_SECONDS:
        return _BAR_SECONDS[bar]
    raise ValueError(f"unsupported bar size {bar!r}")


class StaleGuard:
    """Refuse to trade when candles, clock, or price are not trustworthy.

    The bar interval comes from the strategy timeframe (1H=3600, 4H=14400,
    1Dutc=86400), not from config. ``tolerance_sec`` defaults to 900.

    Freshness uses Sofia's form (b), not form (a):

    * (b, implemented) stale if ``now - last_close_ts > interval + tolerance``,
      where ``last_close_ts`` is the close of the last confirmed bar
      (OKX open timestamp + interval).
    * (a, rejected) stale if ``last_open_ts < floor(now/interval)*interval
      - tolerance``. That marks a 1D bar stale at 00:00:01 UTC, before OKX
      has confirmed the new day's bar, and fails Sofia's test #3. See
      ``test_form_a_fails_just_after_1d_boundary``.

    A missing previous bar (no candle at last.ts - interval) is also stale.
    """

    def __init__(
        self,
        *,
        bar: str,
        tolerance_sec: float = 900,
        max_clock_skew_ms: int,
        max_price_move_pct: Decimal,
        max_gap_bars: int = 1,
    ) -> None:
        self.bar = bar
        self.interval = bar_interval_seconds(bar)
        self.tolerance_sec = float(tolerance_sec)
        self.max_clock_skew_ms = int(max_clock_skew_ms)
        self.max_price_move_pct = Decimal(str(max_price_move_pct))
        self.max_gap_bars = int(max_gap_bars)

    def check(
        self,
        *,
        candles: list[Candle],
        now: datetime,
        server_time_ms: int,
        last_price: Decimal | None,
    ) -> tuple[bool, str]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local_ms = int(now.timestamp() * 1000)
        if abs(int(server_time_ms) - local_ms) > self.max_clock_skew_ms:
            return False, "clock_skew"
        confirmed = [c for c in candles if c.confirmed]
        if not confirmed:
            return False, "stale_candle"
        confirmed = sorted(confirmed, key=lambda c: c.ts)
        last = confirmed[-1]
        last_close_ts = last.ts + timedelta(seconds=self.interval)
        age = (now - last_close_ts).total_seconds()
        if age > self.interval + self.tolerance_sec:
            return False, "stale_candle"
        expected_prev = last.ts - timedelta(seconds=self.interval)
        by_ts = {bar.ts: bar for bar in confirmed}
        if expected_prev not in by_ts:
            return False, "candle_gap"
        limit = timedelta(seconds=self.interval * self.max_gap_bars + 1)
        for prev, nxt in zip(confirmed, confirmed[1:]):
            if nxt.ts - prev.ts > limit:
                return False, "candle_gap"
        if last_price is not None and last.close > 0:
            move = abs(Decimal(str(last_price)) - last.close) / last.close
            if move > self.max_price_move_pct:
                return False, "price_jump"
        return True, "ok"

    def assert_tradeable(
        self,
        *,
        candles: list[Candle],
        now: datetime,
        server_time_ms: int,
        last_price: Decimal | None,
    ) -> None:
        ok, reason = self.check(
            candles=candles, now=now, server_time_ms=server_time_ms, last_price=last_price
        )
        if not ok:
            raise StaleDataError(reason)
