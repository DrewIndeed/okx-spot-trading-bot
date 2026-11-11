from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from okxspot.exchange.paper import PaperExchange, apply_slippage
from okxspot.models import (
    OrderPurpose,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Ticker,
)
from okxspot.orders.ids import make_client_order_id
from tests.conftest import candle, instrument

D = Decimal


def _paper(**overrides: object) -> PaperExchange:
    inst = instrument(
        lot_sz=D("0.1"),
        min_sz=D("0.1"),
        tick_sz=D("0.1"),
        min_notional=D("1"),
    )
    params: dict[str, object] = {
        "quote_ccy": "USDT",
        "cash": D("1000"),
        "taker_fee": D("0.001"),
        "slippage_bps": D("100"),
        "partial_fill_ratio": D("1"),
        "instruments": {"BTC-USDT": inst},
        "immediate": True,
    }
    params.update(overrides)
    return PaperExchange(**params)  # type: ignore[arg-type]


def _ticker(ask: str = "100", bid: str = "100") -> Ticker:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return Ticker("BTC-USDT", D(bid), D(ask), D(ask), now)


def _buy(qty: str, cid: str = "c" + "a" * 31) -> OrderRequest:
    return OrderRequest(
        "BTC-USDT",
        Side.BUY,
        OrderType.MARKET,
        D(qty),
        cid,
        purpose=OrderPurpose.ENTRY,
    )


def test_slippage_direction() -> None:
    assert apply_slippage(D("100"), Side.BUY, D("100")) == D("101")
    assert apply_slippage(D("100"), Side.SELL, D("100")) == D("99")


def test_market_buy_fee_and_slippage() -> None:
    ex = _paper()
    ex.set_ticker(_ticker())
    order = ex.place_order(_buy("2"))
    fills = ex.pop_new_fills()
    assert len(fills) == 1
    assert fills[0].price == D("101")
    assert fills[0].fee == D("2") * D("101") * D("0.001")
    assert ex.cash_available == D("1000") - D("2") * D("101") * D("1.001")
    assert ex.bases["BTC-USDT"] == D("2")
    assert order.status is OrderStatus.FILLED
    view = ex.get_account_view()
    assert view.available_quote == ex.cash_available
    assert view.total_eq is None


def test_idempotent_client_id_does_not_double_fill() -> None:
    ex = _paper()
    ex.set_ticker(_ticker())
    first = ex.place_order(_buy("1", "c" + "b" * 31))
    second = ex.place_order(_buy("1", "c" + "b" * 31))
    assert first.client_order_id == second.client_order_id
    assert len(ex.pop_new_fills()) == 1
    assert ex.bases["BTC-USDT"] == D("1")


def test_partial_fills_then_complete() -> None:
    ex = _paper(immediate=False, slippage_bps=D("0"), taker_fee=D("0"), partial_fill_ratio=D("0.5"))
    order = ex.place_order(_buy("1"))
    assert order.status is OrderStatus.OPEN
    ex.fill_resting(candle(1, "100"))
    assert order.filled_qty == D("0.5")
    assert order.status is OrderStatus.PARTIALLY_FILLED
    ex.fill_resting(candle(2, "100"))
    assert order.filled_qty == D("1")
    assert order.status is OrderStatus.FILLED


def test_insufficient_cash_rejects() -> None:
    ex = _paper(cash=D("0"), slippage_bps=D("0"), taker_fee=D("0"))
    ex.set_ticker(_ticker())
    order = ex.place_order(_buy("1"))
    assert order.status is OrderStatus.REJECTED
    assert ex.pop_new_fills() == []


def test_stop_and_gap_stop() -> None:
    ex = _paper(immediate=False, slippage_bps=D("0"), taker_fee=D("0"))
    ex.bases["BTC-USDT"] = D("1")
    ex.place_algo_stop(
        __import__("okxspot.models", fromlist=["StopRequest"]).StopRequest(
            "BTC-USDT",
            "c" + "d" * 31,
            D("1"),
            D("90"),
        )
    )
    ex.trigger_stops(candle(1, "95", low="89", high="96", open_="95", close="92"))
    fills = ex.pop_new_fills()
    assert len(fills) == 1
    assert fills[0].price == D("90")
    assert ex.bases["BTC-USDT"] == D("0")

    ex.bases["BTC-USDT"] = D("1")
    ex.place_algo_stop(
        __import__("okxspot.models", fromlist=["StopRequest"]).StopRequest(
            "BTC-USDT",
            "c" + "e" * 31,
            D("1"),
            D("90"),
        )
    )
    ex.trigger_stops(candle(2, "80", low="70", high="80", open_="80", close="75"))
    gap = ex.pop_new_fills()
    assert gap[0].price == D("80")


def test_client_order_id_is_stable() -> None:
    first = make_client_order_id("entry", "BTC-USDT", "buy", 10)
    second = make_client_order_id("entry", "BTC-USDT", "buy", 10)
    other = make_client_order_id("entry", "BTC-USDT", "buy", 11)
    assert first == second
    assert first != other
    assert first.isalnum()
    assert len(first) <= 32
