from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from okxspot.models import Instrument


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    if value <= 0:
        return Decimal("0")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def round_qty(qty: Decimal, instrument: Instrument) -> Decimal:
    rounded = floor_to_step(qty, instrument.lot_sz)
    if rounded < instrument.min_sz:
        return Decimal("0")
    return rounded


def round_price(price: Decimal, instrument: Instrument) -> Decimal:
    return floor_to_step(price, instrument.tick_sz)


def meets_min_notional(qty: Decimal, price: Decimal, instrument: Instrument) -> bool:
    if qty < instrument.min_sz:
        return False
    return not (instrument.min_notional > 0 and qty * price < instrument.min_notional)
