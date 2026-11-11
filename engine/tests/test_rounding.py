from __future__ import annotations

from decimal import Decimal

import pytest

from okxspot.risk.rounding import floor_to_step, meets_min_notional, round_price, round_qty
from tests.conftest import instrument

D = Decimal


def test_floor_to_lot() -> None:
    inst = instrument(lot_sz=D("0.001"), min_sz=D("0.001"))
    assert round_qty(D("1.2345"), inst) == D("1.234")


def test_below_min_lot_is_zero() -> None:
    inst = instrument(lot_sz=D("0.01"), min_sz=D("0.01"))
    assert round_qty(D("0.009"), inst) == D("0")


def test_price_floors_to_tick() -> None:
    inst = instrument(tick_sz=D("0.1"))
    assert round_price(D("100.26"), inst) == D("100.2")


def test_step_must_be_positive() -> None:
    with pytest.raises(ValueError):
        floor_to_step(D("1"), D("0"))


def test_min_notional() -> None:
    inst = instrument(min_sz=D("0.001"), min_notional=D("10"))
    assert meets_min_notional(D("0.05"), D("100"), inst) is False
    assert meets_min_notional(D("0.2"), D("100"), inst) is True
    assert meets_min_notional(D("0.0001"), D("100"), inst) is False
