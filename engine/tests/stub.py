from __future__ import annotations

from decimal import Decimal

from okxspot.exchange.base import Exchange
from okxspot.models import (
    AccountView,
    Candle,
    Fill,
    Instrument,
    Order,
    OrderRequest,
    OrderStatus,
    StopOrder,
    StopRequest,
    Ticker,
)


class StubExchange(Exchange):
    def server_time_ms(self) -> int:
        return 0

    def get_instrument(self, inst_id: str) -> Instrument:
        raise NotImplementedError

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        return []

    def get_ticker(self, inst_id: str) -> Ticker:
        raise NotImplementedError

    def get_account_view(self) -> AccountView:
        return AccountView("USDT", Decimal("0"), None)

    def get_permissions(self) -> set[str]:
        return {"read_only", "trade"}

    def place_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError

    def cancel_order(self, inst_id: str, client_order_id: str) -> Order:
        raise NotImplementedError

    def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
        return None

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        return []

    def cancel_open_orders(self, inst_id: str) -> None:
        return None

    def place_algo_stop(self, request: StopRequest) -> StopOrder:
        return StopOrder(
            request.inst_id,
            request.client_order_id,
            request.qty,
            request.stop_price,
            "live",
        )

    def cancel_algos(self, inst_id: str) -> None:
        return None

    def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]:
        return []

    def pop_new_fills(self) -> list[Fill]:
        return []

    def sync(self) -> None:
        return None


def stored_order(request: OrderRequest, status: OrderStatus = OrderStatus.OPEN) -> Order:
    return Order(
        inst_id=request.inst_id,
        side=request.side,
        ord_type=request.ord_type,
        qty=request.qty,
        client_order_id=request.client_order_id,
        status=status,
        price=request.price,
        purpose=request.purpose,
        exchange_order_id="ex-1",
    )
