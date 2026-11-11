from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from okxspot.models import (
    AccountView,
    Candle,
    Fill,
    Instrument,
    Order,
    OrderRequest,
    StopOrder,
    StopRequest,
    Ticker,
)


class Exchange(ABC):
    """Spot trading interface shared by the OKX adapter and the paper simulator."""

    @abstractmethod
    def server_time_ms(self) -> int: ...

    @abstractmethod
    def get_instrument(self, inst_id: str) -> Instrument: ...

    @abstractmethod
    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]: ...

    @abstractmethod
    def get_ticker(self, inst_id: str) -> Ticker: ...

    @abstractmethod
    def get_account_view(self) -> AccountView: ...

    @abstractmethod
    def get_permissions(self) -> set[str]: ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    def cancel_order(self, inst_id: str, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, inst_id: str, client_order_id: str) -> Order | None: ...

    @abstractmethod
    def get_open_orders(self, inst_id: str | None = None) -> list[Order]: ...

    @abstractmethod
    def cancel_open_orders(self, inst_id: str) -> None: ...

    @abstractmethod
    def place_algo_stop(self, request: StopRequest) -> StopOrder: ...

    @abstractmethod
    def cancel_algos(self, inst_id: str) -> None: ...

    @abstractmethod
    def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]: ...

    @abstractmethod
    def pop_new_fills(self) -> list[Fill]: ...

    @abstractmethod
    def sync(self) -> None: ...

    def fill_resting(self, candle: Candle) -> None:
        """Backtest/paper hook. Live adapters fill through the venue."""
        return None

    def trigger_stops(self, candle: Candle) -> None:
        return None

    def fill_at(self, client_order_id: str, price: Decimal) -> None:
        """Force a resting order to fill at ``price``. No-op if already terminal."""
        return None
