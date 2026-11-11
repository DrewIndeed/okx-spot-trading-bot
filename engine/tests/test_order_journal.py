from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from okxspot.errors import AmbiguousOrderError
from okxspot.models import Order, OrderRequest, OrderStatus, OrderType, Side
from okxspot.orders.ids import make_client_order_id
from okxspot.orders.journal import OrderJournal
from okxspot.orders.manager import OrderManager
from okxspot.state import StateStore
from tests.stub import StubExchange, stored_order

D = Decimal


def test_clordid_is_deterministic_per_strategy_bar_symbol_side() -> None:
    a = make_client_order_id("v2-core", "BTC-USDT", "buy", 1_700_000_000, purpose="entry")
    b = make_client_order_id("v2-core", "BTC-USDT", "buy", 1_700_000_000, purpose="entry")
    assert a == b
    assert a.isalnum() and len(a) <= 32
    assert a != make_client_order_id("v2-zara", "BTC-USDT", "buy", 1_700_000_000, purpose="entry")
    assert a != make_client_order_id("v2-core", "ETH-USDT", "buy", 1_700_000_000, purpose="entry")
    assert a != make_client_order_id("v2-core", "BTC-USDT", "sell", 1_700_000_000, purpose="entry")
    assert a != make_client_order_id("v2-core", "BTC-USDT", "buy", 1_700_000_001, purpose="entry")


def test_journal_persists_and_blocks_duplicate_submit(tmp_path: Path) -> None:
    class Counting(StubExchange):
        def __init__(self) -> None:
            self.place_calls = 0
            self.orders: dict[str, Order] = {}

        def place_order(self, request: OrderRequest) -> Order:
            self.place_calls += 1
            order = stored_order(request)
            self.orders[request.client_order_id] = order
            return order

        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            return self.orders.get(client_order_id)

    ex = Counting()
    state = StateStore(tmp_path / "s.db")
    journal = OrderJournal(tmp_path / "orders.jsonl")
    cid = make_client_order_id("core", "BTC-USDT", "buy", 10)
    req = OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), cid)
    mgr = OrderManager(ex, state, journal=journal)
    first = mgr.submit(req)
    # crash: new manager, same journal + state
    state2 = StateStore(tmp_path / "s.db")
    mgr2 = OrderManager(ex, state2, journal=OrderJournal(tmp_path / "orders.jsonl"))
    second = mgr2.submit(req)
    assert first.client_order_id == second.client_order_id
    assert ex.place_calls == 1
    events = OrderJournal(tmp_path / "orders.jsonl").load()
    assert any(e["event"] == "submitted" for e in events)
    state.close()
    state2.close()


def test_timeout_queries_before_retry_and_records_unknown(tmp_path: Path) -> None:
    class TimeoutThenFound(StubExchange):
        def __init__(self) -> None:
            self.place_calls = 0
            self.lookups = 0
            self.stored: Order | None = None

        def place_order(self, request: OrderRequest) -> Order:
            self.place_calls += 1
            if self.place_calls == 1:
                self.stored = stored_order(request, OrderStatus.OPEN)
                raise AmbiguousOrderError("timeout")
            raise AssertionError("must not place again after the order is found")

        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            self.lookups += 1
            return self.stored

    ex = TimeoutThenFound()
    state = StateStore(tmp_path / "s.db")
    journal = OrderJournal(tmp_path / "orders.jsonl")
    req = OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), "c" + "d" * 31)
    order = OrderManager(ex, state, journal=journal).submit(req)
    assert order.status is OrderStatus.OPEN
    assert ex.place_calls == 1
    assert ex.lookups >= 1
    events = [e["event"] for e in journal.load()]
    assert "ambiguous" in events
    assert "queried" in events
    state.close()


def test_partial_fill_and_reject_are_journaled(tmp_path: Path) -> None:
    class Book(StubExchange):
        def __init__(self) -> None:
            self.by_id: dict[str, Order] = {}
            self.open_ids: set[str] = set()

        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            return self.by_id.get(client_order_id)

        def get_open_orders(self, inst_id: str | None = None):  # type: ignore[override]
            return [self.by_id[cid] for cid in self.open_ids]

    ex = Book()
    state = StateStore(tmp_path / "s.db")
    journal = OrderJournal(tmp_path / "orders.jsonl")
    mgr = OrderManager(ex, state, journal=journal)
    partial_req = OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("2"), "c" + "e" * 31)
    partial = stored_order(partial_req, OrderStatus.PARTIALLY_FILLED)
    partial.filled_qty = D("1")
    state.upsert_order(stored_order(partial_req, OrderStatus.OPEN))
    ex.by_id[partial.client_order_id] = partial

    rejected_req = OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), "c" + "f" * 31)
    rejected = stored_order(rejected_req, OrderStatus.REJECTED)
    state.upsert_order(stored_order(rejected_req, OrderStatus.OPEN))
    ex.by_id[rejected.client_order_id] = rejected

    report = mgr.reconcile()
    assert partial.client_order_id in report.updated
    events = journal.load()
    kinds = {(e["event"], e.get("status")) for e in events}
    assert ("reconciled", "partially_filled") in kinds
    assert ("reconciled", "rejected") in kinds
    state.close()


def test_reconcile_mismatch_is_reported(tmp_path: Path) -> None:
    class Book(StubExchange):
        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            return None

        def get_open_orders(self, inst_id: str | None = None):  # type: ignore[override]
            return []

    state = StateStore(tmp_path / "s.db")
    journal = OrderJournal(tmp_path / "orders.jsonl")
    mgr = OrderManager(Book(), state, journal=journal)
    local = stored_order(OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), "c" + "g" * 31), OrderStatus.OPEN)
    state.upsert_order(local)
    report = mgr.reconcile()
    assert local.client_order_id in report.missing
    assert any(e["event"] == "mismatch" for e in journal.load())
    state.close()
