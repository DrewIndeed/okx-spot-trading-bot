from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"
    FLATTEN = "flatten"


OPEN_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
)


def assert_spot_symbol(inst_id: str) -> None:
    """This engine trades OKX spot pairs only (BASE-QUOTE)."""
    if inst_id.count("-") != 1 or "SWAP" in inst_id.upper() or "FUTURES" in inst_id.upper():
        raise ValueError(f"spot-only engine refuses instrument {inst_id}")


@dataclass(frozen=True, slots=True)
class Instrument:
    inst_id: str
    base_ccy: str
    quote_ccy: str
    tick_sz: Decimal
    lot_sz: Decimal
    min_sz: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        assert_spot_symbol(self.inst_id)
        if self.tick_sz <= 0 or self.lot_sz <= 0 or self.min_sz <= 0:
            raise ValueError("instrument steps must be positive")
        if self.min_notional < 0:
            raise ValueError("min_notional cannot be negative")


@dataclass(frozen=True, slots=True)
class Candle:
    inst_id: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    confirmed: bool = True

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("candle timestamp must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"invalid OHLC for {self.inst_id} at {self.ts.isoformat()}")
        if self.low <= 0 or self.volume < 0:
            raise ValueError("price must be positive and volume cannot be negative")


@dataclass(frozen=True, slots=True)
class Ticker:
    inst_id: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class AccountView:
    """Quote cash the sizer is allowed to see.

    ``total_eq`` is the exchange account equity, if the venue reported it.
    Sizing must not read it. It exists so the startup path can warn when a
    shared account holds funds outside the bot's allocated capital.
    """

    quote_ccy: str
    available_quote: Decimal
    total_eq: Decimal | None = None


@dataclass(slots=True)
class OrderRequest:
    inst_id: str
    side: Side
    ord_type: OrderType
    qty: Decimal
    client_order_id: str
    price: Decimal | None = None
    purpose: OrderPurpose = OrderPurpose.ENTRY

    def __post_init__(self) -> None:
        assert_spot_symbol(self.inst_id)
        if self.qty <= 0:
            raise ValueError("order qty must be positive")
        if self.ord_type is OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("limit order requires a positive price")
        if not self.client_order_id.isalnum() or len(self.client_order_id) > 32:
            raise ValueError("client_order_id must be alphanumeric and at most 32 characters")


@dataclass(slots=True)
class Order:
    inst_id: str
    side: Side
    ord_type: OrderType
    qty: Decimal
    client_order_id: str
    status: OrderStatus
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    price: Decimal | None = None
    exchange_order_id: str | None = None
    purpose: OrderPurpose = OrderPurpose.ENTRY
    fee: Decimal = Decimal("0")
    fee_ccy: str = "USDT"
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: str
    client_order_id: str
    inst_id: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_ccy: str
    ts: datetime


@dataclass(frozen=True, slots=True)
class StopRequest:
    inst_id: str
    client_order_id: str
    qty: Decimal
    stop_price: Decimal

    def __post_init__(self) -> None:
        assert_spot_symbol(self.inst_id)
        if self.qty <= 0 or self.stop_price <= 0:
            raise ValueError("stop requires positive qty and stop price")


@dataclass(slots=True)
class StopOrder:
    inst_id: str
    client_order_id: str
    qty: Decimal
    stop_price: Decimal
    status: str
    algo_id: str | None = None
    side: Side = Side.SELL


@dataclass(frozen=True, slots=True)
class Signal:
    inst_id: str
    side: Side
    stop_price: Decimal | None
    reason: str


@dataclass(slots=True)
class Position:
    inst_id: str
    qty: Decimal
    avg_price: Decimal
    stop_price: Decimal | None
    initial_risk_quote: Decimal
    opened_at: datetime
    realized_fragment: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    inst_id: str
    pnl: Decimal
    initial_risk_quote: Decimal
    entry_ts: datetime
    exit_ts: datetime

    @property
    def r_multiple(self) -> Decimal:
        if self.initial_risk_quote == 0:
            return Decimal("0")
        return self.pnl / self.initial_risk_quote

    @property
    def won(self) -> bool:
        return self.pnl > 0
