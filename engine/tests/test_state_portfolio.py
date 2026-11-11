from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from okxspot.errors import StateError
from okxspot.models import Fill, Side
from okxspot.portfolio import PortfolioBook
from okxspot.state import StateStore

D = Decimal
TS = datetime(2024, 1, 2, tzinfo=UTC)


def _fill(trade_id: str, side: Side, qty: str, price: str, fee: str = "0") -> Fill:
    return Fill(trade_id, "c" + "a" * 31, "BTC-USDT", side, D(price), D(qty), D(fee), "USDT", TS)


def test_round_trip_pnl_and_duplicate_fill_ignored() -> None:
    book = PortfolioBook(D("1000"))
    book.apply_fill(_fill("t1", Side.BUY, "2", "100", "0.2"), stop_price=D("90"))
    closed = book.apply_fill(_fill("t2", Side.SELL, "2", "110", "0.22"))
    assert closed is not None
    assert book.positions == {}
    # cost 200.2, proceeds 220 - 0.22 = 219.78, pnl 19.58
    assert book.realized_pnl == D("19.58")
    again = book.apply_fill(_fill("t2", Side.SELL, "2", "110", "0.22"))
    assert again is None
    assert book.realized_pnl == D("19.58")
    assert book.marked_equity({}) == D("1019.58")


def test_book_persists_and_rejects_cap_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "state.db"
    store = StateStore(path)
    book = store.load_book(D("1000"))
    book.apply_fill(_fill("t1", Side.BUY, "1", "100"), stop_price=D("90"))
    store.save_book(book)
    store.close()

    reopened = StateStore(path)
    loaded = reopened.load_book(D("1000"))
    assert loaded.positions["BTC-USDT"].qty == D("1")
    loaded.apply_fill(_fill("t1", Side.BUY, "1", "100"), stop_price=D("90"))
    assert loaded.positions["BTC-USDT"].qty == D("1")
    with pytest.raises(StateError, match="allocated_capital"):
        reopened.load_book(D("2000"))
    reopened.close()
