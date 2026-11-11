from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from okxspot.errors import OkxApiError
from okxspot.models import (
    AccountView,
    Candle,
    Instrument,
    Order,
    OrderPurpose,
    OrderStatus,
    OrderType,
    Side,
    StopOrder,
    Ticker,
    assert_spot_symbol,
)

_ORDER_STATUS = {
    "live": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "mmp_canceled": OrderStatus.CANCELED,
}


def _dec(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _ts(value: object) -> datetime:
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)


def parse_okx_candle_row(inst_id: str, row: list[str]) -> Candle:
    confirmed = True
    if len(row) > 8:
        confirmed = str(row[8]) == "1"
    return Candle(
        inst_id=inst_id,
        ts=_ts(row[0]),
        open=_dec(row[1]),
        high=_dec(row[2]),
        low=_dec(row[3]),
        close=_dec(row[4]),
        volume=_dec(row[5]),
        confirmed=confirmed,
    )


def parse_ticker_row(row: dict[str, str]) -> Ticker:
    inst = row.get("instId") or ""
    assert_spot_symbol(inst)
    return Ticker(
        inst_id=inst,
        bid=_dec(row.get("bidPx")),
        ask=_dec(row.get("askPx")),
        last=_dec(row.get("last")),
        ts=_ts(row.get("ts") or "0"),
    )


def parse_instrument(row: dict[str, str], min_notional: Decimal) -> Instrument:
    inst_type = (row.get("instType") or "SPOT").upper()
    if inst_type != "SPOT":
        raise ValueError(f"refusing non-spot instrument type {inst_type}")
    return Instrument(
        inst_id=row["instId"],
        base_ccy=row["baseCcy"],
        quote_ccy=row["quoteCcy"],
        tick_sz=_dec(row["tickSz"]),
        lot_sz=_dec(row["lotSz"]),
        min_sz=_dec(row["minSz"]),
        min_notional=min_notional,
    )


def parse_permissions(payload: dict[str, object]) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return set()
    first = data[0]
    if not isinstance(first, dict):
        return set()
    raw = first.get("perm")
    if not isinstance(raw, str):
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def parse_account_view(payload: dict[str, object], quote_ccy: str) -> AccountView:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise OkxApiError("balance", "empty balance payload")
    head = data[0]
    total_raw = head.get("totalEq")
    total_eq = _dec(total_raw) if total_raw not in (None, "") else None
    available = Decimal("0")
    details = head.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            balance = Balance(
                ccy=str(item.get("ccy") or ""),
                available=_dec(item.get("availBal")),
                frozen=_dec(item.get("frozenBal")),
            )
            if balance.ccy == quote_ccy:
                available = balance.available
    return AccountView(quote_ccy=quote_ccy, available_quote=available, total_eq=total_eq)


class Balance:
    def __init__(self, ccy: str, available: Decimal, frozen: Decimal) -> None:
        self.ccy = ccy
        self.available = available
        self.frozen = frozen


def parse_order_row(row: dict[str, str], *, purpose: OrderPurpose = OrderPurpose.ENTRY) -> Order:
    side = Side(str(row["side"]))
    ord_type = OrderType(str(row.get("ordType") or "market"))
    state = _ORDER_STATUS.get(str(row.get("state") or ""), OrderStatus.UNKNOWN)
    price_raw = row.get("px")
    price = _dec(price_raw) if price_raw not in (None, "") else None
    return Order(
        inst_id=row["instId"],
        side=side,
        ord_type=ord_type,
        qty=_dec(row.get("sz")),
        client_order_id=str(row.get("clOrdId") or ""),
        status=state,
        filled_qty=_dec(row.get("accFillSz")),
        avg_price=_dec(row.get("avgPx")),
        price=price,
        exchange_order_id=str(row.get("ordId") or "") or None,
        purpose=purpose,
        fee=abs(_dec(row.get("fee"))),
        fee_ccy=str(row.get("feeCcy") or "USDT"),
        updated_at=_ts(row["uTime"]) if row.get("uTime") else None,
    )


def parse_stop_row(row: dict[str, str]) -> StopOrder:
    stop_px = row.get("slTriggerPx") or row.get("triggerPx") or "0"
    status = str(row.get("state") or "live")
    return StopOrder(
        inst_id=row["instId"],
        client_order_id=str(row.get("algoClOrdId") or row.get("clOrdId") or ""),
        qty=_dec(row.get("sz")),
        stop_price=_dec(stop_px),
        status=status,
        algo_id=str(row.get("algoId") or "") or None,
        side=Side(str(row.get("side") or "sell")),
    )


def okx_code(payload: dict[str, object]) -> str:
    code = payload.get("code")
    return "" if code is None else str(code)


def ensure_ok(payload: dict[str, object]) -> dict[str, object]:
    code = okx_code(payload)
    if code not in {"", "0"}:
        msg = payload.get("msg")
        raise OkxApiError(code, str(msg or "okx error"))
    return payload
