from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from okxspot.decimals import dec_str, parse_decimal
from okxspot.errors import StateError
from okxspot.models import (
    Fill,
    Order,
    OrderPurpose,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from okxspot.portfolio import PortfolioBook


class StateStore:
    """SQLite persistence for orders, fills, the bot book, and kill-switch state."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self._conn.close()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                exchange_order_id TEXT,
                inst_id TEXT NOT NULL,
                side TEXT NOT NULL,
                ord_type TEXT NOT NULL,
                purpose TEXT NOT NULL,
                qty TEXT NOT NULL,
                price TEXT,
                filled_qty TEXT NOT NULL,
                avg_price TEXT NOT NULL,
                status TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_ccy TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fills (
                trade_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_ccy TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                inst_id TEXT PRIMARY KEY,
                qty TEXT NOT NULL,
                avg_price TEXT NOT NULL,
                stop_price TEXT,
                initial_risk_quote TEXT NOT NULL,
                realized_fragment TEXT NOT NULL,
                opened_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applied_trades (
                trade_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS order_meta (
                client_order_id TEXT PRIMARY KEY,
                stop_price TEXT,
                risk_amount TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def upsert_order(self, order: Order) -> None:
        self._conn.execute(
            """
            INSERT INTO orders (
                client_order_id, exchange_order_id, inst_id, side, ord_type, purpose,
                qty, price, filled_qty, avg_price, status, fee, fee_ccy, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                exchange_order_id=excluded.exchange_order_id,
                inst_id=excluded.inst_id,
                side=excluded.side,
                ord_type=excluded.ord_type,
                purpose=excluded.purpose,
                qty=excluded.qty,
                price=excluded.price,
                filled_qty=excluded.filled_qty,
                avg_price=excluded.avg_price,
                status=excluded.status,
                fee=excluded.fee,
                fee_ccy=excluded.fee_ccy,
                updated_at=excluded.updated_at
            """,
            (
                order.client_order_id,
                order.exchange_order_id,
                order.inst_id,
                order.side.value,
                order.ord_type.value,
                order.purpose.value,
                dec_str(order.qty),
                "" if order.price is None else dec_str(order.price),
                dec_str(order.filled_qty),
                dec_str(order.avg_price),
                order.status.value,
                dec_str(order.fee),
                order.fee_ccy,
                "" if order.updated_at is None else order.updated_at.isoformat(),
            ),
        )
        self._conn.commit()

    def get_order(self, client_order_id: str) -> Order | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return _order_from_row(row)

    def list_orders(self) -> list[Order]:
        rows = self._conn.execute("SELECT * FROM orders").fetchall()
        return [_order_from_row(row) for row in rows]

    def save_fill(self, fill: Fill) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fills (
                trade_id, client_order_id, inst_id, side, price, qty, fee, fee_ccy, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.trade_id,
                fill.client_order_id,
                fill.inst_id,
                fill.side.value,
                dec_str(fill.price),
                dec_str(fill.qty),
                dec_str(fill.fee),
                fill.fee_ccy,
                fill.ts.isoformat(),
            ),
        )
        self._conn.commit()

    def save_meta(
        self,
        client_order_id: str,
        stop_price: Decimal | None,
        risk_amount: Decimal | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO order_meta (client_order_id, stop_price, risk_amount)
            VALUES (?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                stop_price=excluded.stop_price,
                risk_amount=excluded.risk_amount
            """,
            (
                client_order_id,
                "" if stop_price is None else dec_str(stop_price),
                "" if risk_amount is None else dec_str(risk_amount),
            ),
        )
        self._conn.commit()

    def load_meta(self) -> dict[str, tuple[Decimal | None, Decimal | None]]:
        rows = self._conn.execute("SELECT * FROM order_meta").fetchall()
        out: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        for row in rows:
            stop = parse_decimal(row["stop_price"]) if row["stop_price"] else None
            risk = parse_decimal(row["risk_amount"]) if row["risk_amount"] else None
            out[str(row["client_order_id"])] = (stop, risk)
        return out

    def set_kv(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO kv (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def get_kv(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def save_book(self, book: PortfolioBook) -> None:
        self.set_kv("allocated_capital", dec_str(book.allocated_capital) or "0")
        self.set_kv("realized_pnl", dec_str(book.realized_pnl) or "0")
        self._conn.execute("DELETE FROM positions")
        for pos in book.positions.values():
            self._conn.execute(
                """
                INSERT INTO positions (
                    inst_id, qty, avg_price, stop_price, initial_risk_quote,
                    realized_fragment, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.inst_id,
                    dec_str(pos.qty),
                    dec_str(pos.avg_price),
                    "" if pos.stop_price is None else dec_str(pos.stop_price),
                    dec_str(pos.initial_risk_quote),
                    dec_str(pos.realized_fragment),
                    pos.opened_at.isoformat(),
                ),
            )
        self._conn.execute("DELETE FROM applied_trades")
        for trade_id in book.applied:
            self._conn.execute("INSERT INTO applied_trades (trade_id) VALUES (?)", (trade_id,))
        self._conn.commit()

    def load_book(self, allocated_capital: Decimal) -> PortfolioBook:
        stored = self.get_kv("allocated_capital")
        if stored is None:
            book = PortfolioBook(allocated_capital)
            self.set_kv("allocated_capital", dec_str(allocated_capital) or "0")
            self.set_kv("realized_pnl", "0")
            return book
        if parse_decimal(stored) != allocated_capital:
            raise StateError(
                f"allocated_capital mismatch: state has {stored}, config has {allocated_capital}"
            )
        book = PortfolioBook(allocated_capital)
        realized = self.get_kv("realized_pnl")
        book.realized_pnl = parse_decimal(realized or "0")
        for row in self._conn.execute("SELECT * FROM positions").fetchall():
            stop_raw = str(row["stop_price"] or "")
            pos = Position(
                inst_id=str(row["inst_id"]),
                qty=parse_decimal(row["qty"]),
                avg_price=parse_decimal(row["avg_price"]),
                stop_price=parse_decimal(stop_raw) if stop_raw else None,
                initial_risk_quote=parse_decimal(row["initial_risk_quote"]),
                opened_at=datetime.fromisoformat(str(row["opened_at"])),
                realized_fragment=parse_decimal(row["realized_fragment"]),
            )
            book.positions[pos.inst_id] = pos
        for row in self._conn.execute("SELECT trade_id FROM applied_trades").fetchall():
            book.applied.add(str(row["trade_id"]))
        return book

    def save_paper_balances(self, cash: Decimal, bases: dict[str, Decimal]) -> None:
        payload = {inst: dec_str(qty) for inst, qty in bases.items()}
        self.set_kv("paper_cash", dec_str(cash) or "0")
        self.set_kv("paper_bases", json.dumps(payload))

    def load_paper_balances(self) -> tuple[Decimal, dict[str, Decimal]] | None:
        cash = self.get_kv("paper_cash")
        if cash is None:
            return None
        raw = self.get_kv("paper_bases") or "{}"
        loaded = json.loads(raw)
        bases: dict[str, Decimal] = {}
        if isinstance(loaded, dict):
            for inst, qty in loaded.items():
                bases[str(inst)] = parse_decimal(qty)
        return parse_decimal(cash), bases


def _order_from_row(row: sqlite3.Row) -> Order:
    price_raw = str(row["price"] or "")
    updated = str(row["updated_at"] or "")
    return Order(
        inst_id=str(row["inst_id"]),
        side=Side(str(row["side"])),
        ord_type=OrderType(str(row["ord_type"])),
        qty=parse_decimal(row["qty"]),
        client_order_id=str(row["client_order_id"]),
        status=OrderStatus(str(row["status"])),
        filled_qty=parse_decimal(row["filled_qty"]),
        avg_price=parse_decimal(row["avg_price"]),
        price=parse_decimal(price_raw) if price_raw else None,
        exchange_order_id=str(row["exchange_order_id"] or "") or None,
        purpose=OrderPurpose(str(row["purpose"])),
        fee=parse_decimal(row["fee"]),
        fee_ccy=str(row["fee_ccy"]),
        updated_at=datetime.fromisoformat(updated) if updated else None,
    )
