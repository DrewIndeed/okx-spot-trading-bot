from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from okxspot.models import AccountView, Side, Signal
from okxspot.risk.equity import equity_from_view, sizing_equity
from okxspot.risk.manager import RiskManager
from tests.conftest import candle, instrument, make_settings

D = Decimal


def test_signature_has_no_account_total() -> None:
    params = set(inspect.signature(sizing_equity).parameters)
    assert params == {"allocated_capital", "realized_pnl", "available_quote"}


def test_sizing_equity_caps_at_allocated_not_available_or_total() -> None:
    # Available and a hypothetical account total are far above the cap.
    assert sizing_equity(
        allocated_capital=D("1000"),
        realized_pnl=D("0"),
        available_quote=D("50000"),
    ) == D("1000")


def test_sizing_equity_uses_available_as_ceiling() -> None:
    assert sizing_equity(
        allocated_capital=D("1000"),
        realized_pnl=D("50"),
        available_quote=D("800"),
    ) == D("800")


def test_sizing_equity_shrinks_after_realized_loss() -> None:
    assert sizing_equity(
        allocated_capital=D("1000"),
        realized_pnl=D("-100"),
        available_quote=D("5000"),
    ) == D("900")


def test_sizing_equity_floors_at_zero_when_losses_wipe_the_cap() -> None:
    assert sizing_equity(
        allocated_capital=D("1000"),
        realized_pnl=D("-5000"),
        available_quote=D("5000"),
    ) == D("0")


def test_negative_available_is_rejected() -> None:
    with pytest.raises(ValueError, match="available_quote"):
        sizing_equity(
            allocated_capital=D("1000"),
            realized_pnl=D("0"),
            available_quote=D("-1"),
        )


def test_view_ignores_total_eq() -> None:
    view = AccountView("USDT", D("50000"), total_eq=D("999999"))
    assert equity_from_view(D("1000"), D("0"), view) == D("1000")
    tight = AccountView("USDT", D("800"), total_eq=D("999999"))
    assert equity_from_view(D("1000"), D("50"), tight) == D("800")


def test_order_qty_uses_allocated_cap_not_surplus_cash() -> None:
    risk = RiskManager(make_settings().risk)
    decision = risk.evaluate_entry(
        entry_price=D("100"),
        stop_price=D("95"),
        allocated_capital=D("1000"),
        realized_pnl=D("0"),
        available_quote=D("50000"),
        open_positions=0,
        current_exposure_notional=D("0"),
        instrument=instrument(),
        has_position=False,
        fee_rate=D("0"),
    )
    # 1% of 1000 = 10 USDT risk, stop distance 5 -> qty 2.
    # 1% of 50000 would be qty 100.
    assert decision.allowed
    assert decision.sizing_equity_used == D("1000")
    assert decision.qty == D("2")


def test_session_does_not_size_from_surplus_cash_or_total_eq() -> None:
    from okxspot.backtest.session import run_session

    class Script:
        name = "script"

        def on_bar(self, ctx: object) -> list[Signal]:
            from okxspot.strategy.base import BarContext

            assert isinstance(ctx, BarContext)
            if ctx.now == candle(0).ts and ctx.position is None:
                return [Signal("BTC-USDT", Side.BUY, D("99"), "test")]
            return []

    settings = make_settings(
        fees={
            "taker_fee": "0",
            "backtest_fee_multiplier": "1",
            "paper_fee_multiplier": "1",
            "slippage_bps": "0",
            "partial_fill_ratio": "1",
        }
    )
    candles = {
        "BTC-USDT": [candle(0, "100"), candle(1, "100")],
    }
    result = run_session(
        candles,
        settings,
        fee_rate=D("0"),
        strategy=Script(),
        starting_cash=D("50000"),
        total_eq_decoy=D("999999"),
    )
    position = result.engine.book.positions["BTC-USDT"]
    # Distance 1, risk budget 10 -> qty 10. Surplus cash would allow qty 500.
    assert position.qty == D("10")
