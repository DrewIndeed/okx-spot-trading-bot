from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okxspot.models import Candle


def synthetic_market(n: int = 260) -> dict[str, list[Candle]]:
    """Deterministic daily bars for offline paper and backtest demos."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return {
        "BTC-USDT": _trend("BTC-USDT", start, n, Decimal("40000"), Decimal("40"), Decimal("180")),
        "ETH-USDT": _trend("ETH-USDT", start, n, Decimal("2200"), Decimal("2"), Decimal("12")),
    }


def _trend(
    inst_id: str,
    start: datetime,
    n: int,
    price0: Decimal,
    drift: Decimal,
    vol: Decimal,
) -> list[Candle]:
    price = price0
    candles: list[Candle] = []
    for index in range(n):
        wobble = vol * Decimal(str(round(math.sin(index / 5.0), 6)))
        open_ = price
        close = price + drift + wobble
        if close <= 1:
            close = Decimal("1")
        high = max(open_, close) + vol / Decimal("4")
        low = min(open_, close) - vol / Decimal("4")
        if low <= 0:
            low = Decimal("0.5")
        candles.append(
            Candle(
                inst_id=inst_id,
                ts=start + timedelta(days=index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("10"),
                confirmed=True,
            )
        )
        price = close
    return candles
