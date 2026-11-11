from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from okxspot.exchange.base import Exchange
from okxspot.models import (
    OPEN_STATUSES,
    AccountView,
    Candle,
    Fill,
    Instrument,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    StopOrder,
    StopRequest,
    Ticker,
)
from okxspot.risk.rounding import floor_to_step


def apply_slippage(price: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    slip = slippage_bps / Decimal("10000")
    if side is Side.BUY:
        return price * (Decimal("1") + slip)
    return price * (Decimal("1") - slip)


class PaperExchange(Exchange):
    """Local spot simulator with the same interface as the OKX adapter.

    Fees are charged in quote. Buys spend ``qty * price + fee``. Sells credit
    ``qty * price - fee``. Partial fills take ``partial_fill_ratio`` of the
    original qty on each match until the order is done. Market orders in bar
    mode rest until the next candle open. ``total_eq`` on the account view is
    a decoy and is not a cash balance.
    """

    def __init__(
        self,
        *,
        quote_ccy: str,
        cash: Decimal,
        taker_fee: Decimal,
        slippage_bps: Decimal,
        partial_fill_ratio: Decimal,
        instruments: dict[str, Instrument],
        immediate: bool = False,
        total_eq_decoy: Decimal | None = None,
    ) -> None:
        if taker_fee < 0 or slippage_bps < 0:
            raise ValueError("fee and slippage cannot be negative")
        if partial_fill_ratio <= 0 or partial_fill_ratio > 1:
            raise ValueError("partial_fill_ratio must be in (0, 1]")
        self.quote_ccy = quote_ccy
        self.cash_available = cash
        self.taker_fee = taker_fee
        self.slippage_bps = slippage_bps
        self.partial_fill_ratio = partial_fill_ratio
        self.instruments = instruments
        self.immediate = immediate
        self.total_eq_decoy = total_eq_decoy
        self.bases: dict[str, Decimal] = {}
        self._orders: dict[str, Order] = {}
        self._stops: dict[str, StopOrder] = {}
        self._fills: list[Fill] = []
        self._new: list[Fill] = []
        self._tickers: dict[str, Ticker] = {}
        self._last_price: dict[str, Decimal] = {}
        self._seq = 0
        self.permissions = {"read_only", "trade"}

    def server_time_ms(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    def get_instrument(self, inst_id: str) -> Instrument:
        try:
            return self.instruments[inst_id]
        except KeyError as exc:
            raise KeyError(f"no instrument info for {inst_id}") from exc

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        return []

    def set_ticker(self, ticker: Ticker) -> None:
        self._tickers[ticker.inst_id] = ticker
        self._last_price[ticker.inst_id] = ticker.last

    def get_ticker(self, inst_id: str) -> Ticker:
        ticker = self._tickers.get(inst_id)
        if ticker is None:
            raise KeyError(f"no ticker for {inst_id}")
        return ticker

    def get_account_view(self) -> AccountView:
        return AccountView(
            quote_ccy=self.quote_ccy,
            available_quote=self.cash_available,
            total_eq=self.total_eq_decoy,
        )

    def get_permissions(self) -> set[str]:
        return set(self.permissions)

    def place_order(self, request: OrderRequest) -> Order:
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            return existing
        order = Order(
            inst_id=request.inst_id,
            side=request.side,
            ord_type=request.ord_type,
            qty=request.qty,
            client_order_id=request.client_order_id,
            status=OrderStatus.OPEN,
            price=request.price,
            exchange_order_id=self._next_id("ord"),
            purpose=request.purpose,
            fee_ccy=self.quote_ccy,
            updated_at=datetime.now(UTC),
        )
        self._orders[order.client_order_id] = order
        if self.immediate and request.ord_type is OrderType.MARKET:
            ticker = self._tickers.get(request.inst_id)
            if ticker is None:
                order.status = OrderStatus.REJECTED
                return order
            raw = ticker.ask if request.side is Side.BUY else ticker.bid
            self._execute(order, apply_slippage(raw, request.side, self.slippage_bps), ticker.ts)
        return order

    def cancel_order(self, inst_id: str, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None or order.inst_id != inst_id:
            raise KeyError(client_order_id)
        if order.status in OPEN_STATUSES:
            order.status = OrderStatus.CANCELED
            order.updated_at = datetime.now(UTC)
        return order

    def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None or order.inst_id != inst_id:
            return None
        return order

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        out = [order for order in self._orders.values() if order.status in OPEN_STATUSES]
        if inst_id is not None:
            out = [order for order in out if order.inst_id == inst_id]
        return out

    def cancel_open_orders(self, inst_id: str) -> None:
        for order in self.get_open_orders(inst_id):
            order.status = OrderStatus.CANCELED

    def place_algo_stop(self, request: StopRequest) -> StopOrder:
        existing = self._stops.get(request.client_order_id)
        if existing is not None:
            return existing
        stop = StopOrder(
            inst_id=request.inst_id,
            client_order_id=request.client_order_id,
            qty=request.qty,
            stop_price=request.stop_price,
            status="live",
            algo_id=self._next_id("algo"),
        )
        self._stops[request.client_order_id] = stop
        return stop

    def cancel_algos(self, inst_id: str) -> None:
        for stop in self._stops.values():
            if stop.inst_id == inst_id and stop.status == "live":
                stop.status = "canceled"

    def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]:
        out = [stop for stop in self._stops.values() if stop.status == "live"]
        if inst_id is not None:
            out = [stop for stop in out if stop.inst_id == inst_id]
        return out

    def pop_new_fills(self) -> list[Fill]:
        out = list(self._new)
        self._new.clear()
        return out

    def sync(self) -> None:
        return None

    def fill_resting(self, candle: Candle) -> None:
        self._last_price[candle.inst_id] = candle.close
        for order in list(self._orders.values()):
            if order.inst_id != candle.inst_id or order.status not in OPEN_STATUSES:
                continue
            raw = self._raw_fill_price(order, candle)
            if raw is None:
                continue
            price = raw
            if order.ord_type is OrderType.MARKET:
                price = apply_slippage(raw, order.side, self.slippage_bps)
            self._execute(order, price, candle.ts)

    def trigger_stops(self, candle: Candle) -> None:
        for stop in list(self._stops.values()):
            if stop.inst_id != candle.inst_id or stop.status != "live":
                continue
            if candle.low > stop.stop_price:
                continue
            raw = candle.open if candle.open < stop.stop_price else stop.stop_price
            price = apply_slippage(raw, Side.SELL, self.slippage_bps)
            self._execute_stop(stop, price, candle.ts)

    def fill_at(self, client_order_id: str, price: Decimal) -> None:
        order = self._orders.get(client_order_id)
        if order is None or order.status not in OPEN_STATUSES:
            return
        fill_price = apply_slippage(price, order.side, self.slippage_bps)
        now = datetime.now(UTC)
        self._execute(order, fill_price, now)

    def _raw_fill_price(self, order: Order, candle: Candle) -> Decimal | None:
        if order.ord_type is OrderType.MARKET:
            return candle.open
        if order.price is None:
            return None
        if order.side is Side.BUY and candle.low <= order.price:
            return min(order.price, candle.open)
        if order.side is Side.SELL and candle.high >= order.price:
            return max(order.price, candle.open)
        return None

    def _execute(self, order: Order, price: Decimal, ts: datetime) -> None:
        if price <= 0 or order.status not in OPEN_STATUSES:
            return
        remaining = order.qty - order.filled_qty
        chunk = self._chunk(order, remaining)
        if chunk <= 0:
            return
        qty = self._cap_qty(order, chunk, price)
        if qty <= 0:
            if order.filled_qty == 0:
                order.status = OrderStatus.REJECTED
            return
        fee = qty * price * self.taker_fee
        if order.side is Side.BUY:
            self.cash_available -= qty * price + fee
            self.bases[order.inst_id] = self.bases.get(order.inst_id, Decimal("0")) + qty
        else:
            self.bases[order.inst_id] = self.bases.get(order.inst_id, Decimal("0")) - qty
            self.cash_available += qty * price - fee
        self._record_fill(order, qty, price, fee, ts)

    def _execute_stop(self, stop: StopOrder, price: Decimal, ts: datetime) -> None:
        base = self.bases.get(stop.inst_id, Decimal("0"))
        instrument = self.instruments[stop.inst_id]
        qty = floor_to_step(min(stop.qty, base), instrument.lot_sz)
        if qty <= 0 or price <= 0:
            stop.status = "canceled"
            return
        fee = qty * price * self.taker_fee
        self.bases[stop.inst_id] = base - qty
        self.cash_available += qty * price - fee
        stop.status = "filled"
        fill = Fill(
            trade_id=self._next_id("fill"),
            client_order_id=stop.client_order_id,
            inst_id=stop.inst_id,
            side=Side.SELL,
            price=price,
            qty=qty,
            fee=fee,
            fee_ccy=self.quote_ccy,
            ts=ts,
        )
        self._fills.append(fill)
        self._new.append(fill)

    def _chunk(self, order: Order, remaining: Decimal) -> Decimal:
        if self.partial_fill_ratio >= 1:
            return remaining
        instrument = self.instruments[order.inst_id]
        target = floor_to_step(order.qty * self.partial_fill_ratio, instrument.lot_sz)
        if target <= 0:
            target = min(remaining, instrument.lot_sz)
        return min(remaining, target)

    def _cap_qty(self, order: Order, qty: Decimal, price: Decimal) -> Decimal:
        instrument = self.instruments[order.inst_id]
        if order.side is Side.BUY:
            unit = price * (Decimal("1") + self.taker_fee)
            if unit <= 0:
                return Decimal("0")
            affordable = floor_to_step(self.cash_available / unit, instrument.lot_sz)
            qty = min(qty, affordable)
        else:
            held = self.bases.get(order.inst_id, Decimal("0"))
            qty = min(qty, floor_to_step(held, instrument.lot_sz))
        if qty < instrument.min_sz:
            return Decimal("0")
        return qty

    def _record_fill(self, order: Order, qty: Decimal, price: Decimal, fee: Decimal, ts: datetime) -> None:
        new_filled = order.filled_qty + qty
        order.avg_price = (order.avg_price * order.filled_qty + price * qty) / new_filled
        order.filled_qty = new_filled
        order.fee += fee
        order.updated_at = ts
        if order.filled_qty >= order.qty:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        fill = Fill(
            trade_id=self._next_id("fill"),
            client_order_id=order.client_order_id,
            inst_id=order.inst_id,
            side=order.side,
            price=price,
            qty=qty,
            fee=fee,
            fee_ccy=self.quote_ccy,
            ts=ts,
        )
        self._fills.append(fill)
        self._new.append(fill)

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"paper-{prefix}-{self._seq}"
