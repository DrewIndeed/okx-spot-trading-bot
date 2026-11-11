from __future__ import annotations

from decimal import Decimal

import pytest

from okxspot.errors import AmbiguousOrderError
from okxspot.models import OrderRequest, OrderStatus, OrderType, Side
from okxspot.orders.manager import OrderManager
from okxspot.state import StateStore
from tests.stub import StubExchange, stored_order

D = Decimal


def _req(cid: str = "c" + "1" * 31) -> OrderRequest:
    return OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), cid)


class Counting(StubExchange):
    def __init__(self) -> None:
        self.place_calls = 0
        self.orders: dict[str, object] = {}

    def place_order(self, request: OrderRequest):  # type: ignore[override]
        self.place_calls += 1
        order = stored_order(request)
        self.orders[request.client_order_id] = order
        return order

    def get_order(self, inst_id: str, client_order_id: str):  # type: ignore[override]
        return self.orders.get(client_order_id)


class FlakyFound(StubExchange):
    def __init__(self) -> None:
        self.place_calls = 0
        self.stored = None

    def place_order(self, request: OrderRequest):  # type: ignore[override]
        self.place_calls += 1
        self.stored = stored_order(request)
        raise AmbiguousOrderError("timeout")

    def get_order(self, inst_id: str, client_order_id: str):  # type: ignore[override]
        return self.stored


class FlakyMissing(StubExchange):
    def __init__(self) -> None:
        self.place_calls = 0
        self.stored = None
        self.lookups = 0

    def place_order(self, request: OrderRequest):  # type: ignore[override]
        self.place_calls += 1
        if self.place_calls == 1:
            raise AmbiguousOrderError("timeout")
        self.stored = stored_order(request)
        return self.stored

    def get_order(self, inst_id: str, client_order_id: str):  # type: ignore[override]
        self.lookups += 1
        return self.stored


class Book(StubExchange):
    def __init__(self) -> None:
        self.by_id: dict[str, object] = {}
        self.open_ids: set[str] = set()

    def get_order(self, inst_id: str, client_order_id: str):  # type: ignore[override]
        return self.by_id.get(client_order_id)

    def get_open_orders(self, inst_id: str | None = None):  # type: ignore[override]
        return [self.by_id[cid] for cid in self.open_ids]


def test_submit_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ex = Counting()
    state = StateStore(tmp_path / "s.db")
    mgr = OrderManager(ex, state)
    first = mgr.submit(_req())
    second = mgr.submit(_req())
    assert first.client_order_id == second.client_order_id
    assert ex.place_calls == 1
    state.close()


def test_ambiguous_submit_returns_existing_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ex = FlakyFound()
    state = StateStore(tmp_path / "s.db")
    order = OrderManager(ex, state).submit(_req())
    assert order.status is OrderStatus.OPEN
    assert ex.place_calls == 1
    state.close()


def test_ambiguous_submit_retries_once_when_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Sofia M2: after AmbiguousOrderError, park UNKNOWN and do not resend.
    ex = FlakyMissing()
    state = StateStore(tmp_path / "s.db")
    order = OrderManager(ex, state).submit(_req())
    assert order.client_order_id.startswith("c")
    assert ex.place_calls == 1
    assert order.status is OrderStatus.UNKNOWN
    state.close()


def test_reconcile_adopt_fill_and_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ex = Book()
    state = StateStore(tmp_path / "s.db")
    mgr = OrderManager(ex, state)
    local = stored_order(_req("c" + "a" * 31), OrderStatus.OPEN)
    state.upsert_order(local)
    filled = stored_order(_req("c" + "a" * 31), OrderStatus.FILLED)
    ex.by_id[local.client_order_id] = filled

    remote = stored_order(_req("c" + "b" * 31), OrderStatus.OPEN)
    ex.by_id[remote.client_order_id] = remote
    ex.open_ids.add(remote.client_order_id)

    gone = stored_order(_req("c" + "c" * 31), OrderStatus.OPEN)
    state.upsert_order(gone)

    report = mgr.reconcile()
    assert remote.client_order_id in report.adopted
    assert local.client_order_id in report.filled
    assert gone.client_order_id in report.missing
    assert state.get_order(gone.client_order_id).status is OrderStatus.UNKNOWN  # type: ignore[union-attr]
    assert state.get_order(local.client_order_id).status is OrderStatus.FILLED  # type: ignore[union-attr]
    state.close()


def test_second_ambiguous_failure_propagates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Always(StubExchange):
        def place_order(self, request: OrderRequest):  # type: ignore[override]
            raise AmbiguousOrderError("down")

    state = StateStore(tmp_path / "s.db")
    order = OrderManager(Always(), state).submit(_req())
    assert order.status is OrderStatus.UNKNOWN
    state.close()
