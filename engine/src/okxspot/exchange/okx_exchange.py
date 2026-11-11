from __future__ import annotations

import os
from datetime import UTC
from decimal import Decimal
from typing import Any

from okxspot.errors import AmbiguousOrderError, OkxApiError
from okxspot.exchange.base import Exchange
from okxspot.exchange.okx_client import OkxClient
from okxspot.exchange.okx_parse import (
    ensure_ok,
    parse_account_view,
    parse_instrument,
    parse_okx_candle_row,
    parse_order_row,
    parse_permissions,
    parse_stop_row,
    parse_ticker_row,
)
from okxspot.models import (
    OPEN_STATUSES,
    AccountView,
    Candle,
    Fill,
    Instrument,
    Order,
    OrderPurpose,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    StopOrder,
    StopRequest,
    Ticker,
    assert_spot_symbol,
)
from okxspot.safety import NETWORK_DISABLE_ENV

_DUPLICATE_CLIENT_ID = {"51016"}


class OkxExchange(Exchange):
    """OKX v5 spot adapter. Order requests use ``tdMode=cash`` only.

    Designed to run against a dedicated sub-account. ``totalEq`` is parsed so
    the engine can warn about surplus funds; sizing never receives it as the
    risk budget.
    """

    def __init__(
        self,
        client: OkxClient,
        *,
        quote_ccy: str = "USDT",
        min_notional: Decimal = Decimal("1"),
        bar: str = "1D",
    ) -> None:
        self.client = client
        self.quote_ccy = quote_ccy
        self.min_notional = min_notional
        self.bar = bar
        self._instruments: dict[str, Instrument] = {}
        self._seen_trades: set[str] = set()
        self._new: list[Fill] = []
        self._purposes: dict[str, OrderPurpose] = {}

    def server_time_ms(self) -> int:
        payload = self.client.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
        data = payload["data"]
        return int(str(data[0]["ts"]))

    def get_instrument(self, inst_id: str) -> Instrument:
        assert_spot_symbol(inst_id)
        cached = self._instruments.get(inst_id)
        if cached is not None:
            return cached
        payload = self.client.request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst_id},
            auth=False,
            idempotent=True,
        )
        ensure_ok(payload)
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise OkxApiError("instrument", f"no spot instrument {inst_id}")
        row = rows[0]
        if not isinstance(row, dict):
            raise OkxApiError("instrument", "bad instrument payload")
        instrument = parse_instrument({str(k): str(v) for k, v in row.items()}, self.min_notional)
        self._instruments[inst_id] = instrument
        return instrument

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        assert_spot_symbol(inst_id)
        payload = self.client.request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit)},
            auth=False,
            idempotent=True,
        )
        return self._candles_from(inst_id, payload)

    def fetch_history_page(
        self,
        inst_id: str,
        bar: str,
        after_ms: int | None,
        limit: int,
    ) -> list[list[str]]:
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after_ms is not None:
            params["after"] = str(after_ms)
        payload = self.client.request(
            "GET",
            "/api/v5/market/history-candles",
            params=params,
            auth=False,
            idempotent=True,
        )
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [[str(cell) for cell in row] for row in data if isinstance(row, list)]

    def get_ticker(self, inst_id: str) -> Ticker:
        payload = self.client.request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": inst_id},
            auth=False,
            idempotent=True,
        )
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise OkxApiError("ticker", f"no ticker for {inst_id}")
        row = {str(k): str(v) for k, v in data[0].items()}
        return parse_ticker_row(row)

    def get_account_view(self) -> AccountView:
        payload = self.client.request(
            "GET",
            "/api/v5/account/balance",
            params={"ccy": self.quote_ccy},
            auth=True,
            idempotent=True,
        )
        ensure_ok(payload)
        return parse_account_view(payload, self.quote_ccy)

    def get_permissions(self) -> set[str]:
        payload = self.client.request("GET", "/api/v5/account/config", auth=True, idempotent=True)
        ensure_ok(payload)
        return parse_permissions(payload)

    def place_order(self, request: OrderRequest) -> Order:
        self._refuse_if_unmocked()
        assert_spot_symbol(request.inst_id)
        body: dict[str, Any] = {
            "instId": request.inst_id,
            "tdMode": "cash",
            "side": request.side.value,
            "ordType": request.ord_type.value,
            "sz": format(request.qty, "f"),
            "clOrdId": request.client_order_id,
            "tgtCcy": "base_ccy",
        }
        if request.ord_type is OrderType.LIMIT:
            body["px"] = format(request.price or Decimal("0"), "f")
        self._purposes[request.client_order_id] = request.purpose
        try:
            payload = self.client.request(
                "POST",
                "/api/v5/trade/order",
                body=body,
                auth=True,
                idempotent=False,
            )
        except (OkxApiError, AmbiguousOrderError) as exc:
            if isinstance(exc, OkxApiError) and exc.code not in _DUPLICATE_CLIENT_ID:
                raise
            existing = self.get_order(request.inst_id, request.client_order_id)
            if existing is not None:
                return existing
            raise
        self._raise_on_item_error(payload, request.inst_id, request.client_order_id)
        fetched = self.get_order(request.inst_id, request.client_order_id)
        if fetched is not None:
            return fetched
        item = _first_dict(payload)
        return Order(
            inst_id=request.inst_id,
            side=request.side,
            ord_type=request.ord_type,
            qty=request.qty,
            client_order_id=request.client_order_id,
            status=OrderStatus.OPEN,
            price=request.price,
            exchange_order_id=str(item.get("ordId") or "") or None,
            purpose=request.purpose,
        )

    def cancel_order(self, inst_id: str, client_order_id: str) -> Order:
        self.client.request(
            "POST",
            "/api/v5/trade/cancel-order",
            body={"instId": inst_id, "clOrdId": client_order_id},
            auth=True,
            idempotent=True,
        )
        found = self.get_order(inst_id, client_order_id)
        if found is None:
            raise OkxApiError("cancel", f"order {client_order_id} missing after cancel")
        return found

    def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
        payload = self.client.request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": inst_id, "clOrdId": client_order_id},
            auth=True,
            idempotent=True,
        )
        if str(payload.get("code")) == "51603":
            return None
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        row = data[0]
        if not isinstance(row, dict):
            return None
        purpose = self._purposes.get(client_order_id, OrderPurpose.ENTRY)
        return parse_order_row({str(k): str(v) for k, v in row.items()}, purpose=purpose)

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        params = {"instType": "SPOT"}
        if inst_id is not None:
            params["instId"] = inst_id
        payload = self.client.request(
            "GET",
            "/api/v5/trade/orders-pending",
            params=params,
            auth=True,
            idempotent=True,
        )
        ensure_ok(payload)
        return self._orders_from(payload)

    def cancel_open_orders(self, inst_id: str) -> None:
        for order in self.get_open_orders(inst_id):
            if order.status in OPEN_STATUSES:
                self.cancel_order(inst_id, order.client_order_id)

    def place_algo_stop(self, request: StopRequest) -> StopOrder:
        self._refuse_if_unmocked()
        body = {
            "instId": request.inst_id,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "conditional",
            "sz": format(request.qty, "f"),
            "algoClOrdId": request.client_order_id,
            "slTriggerPx": format(request.stop_price, "f"),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        }
        payload = self.client.request(
            "POST",
            "/api/v5/trade/order-algo",
            body=body,
            auth=True,
            idempotent=False,
        )
        ensure_ok(payload)
        item = _first_dict(payload)
        if str(item.get("sCode") or "0") not in {"0", ""}:
            raise OkxApiError(str(item.get("sCode")), str(item.get("sMsg") or "algo rejected"))
        return StopOrder(
            inst_id=request.inst_id,
            client_order_id=request.client_order_id,
            qty=request.qty,
            stop_price=request.stop_price,
            status="live",
            algo_id=str(item.get("algoId") or "") or None,
            side=Side.SELL,
        )

    def cancel_algos(self, inst_id: str) -> None:
        open_stops = self.get_open_algos(inst_id)
        if not open_stops:
            return
        body = [
            {"algoId": stop.algo_id, "instId": inst_id}
            for stop in open_stops
            if stop.algo_id
        ]
        if not body:
            return
        self.client.request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=body,
            auth=True,
            idempotent=True,
        )

    def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]:
        params = {"ordType": "conditional", "instType": "SPOT"}
        if inst_id is not None:
            params["instId"] = inst_id
        payload = self.client.request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params=params,
            auth=True,
            idempotent=True,
        )
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        out: list[StopOrder] = []
        for row in data:
            if isinstance(row, dict):
                out.append(parse_stop_row({str(k): str(v) for k, v in row.items()}))
        return [stop for stop in out if stop.status in {"live", "effective", "partially_effective"}]

    def pop_new_fills(self) -> list[Fill]:
        out = list(self._new)
        self._new.clear()
        return out

    def sync(self) -> None:
        payload = self.client.request(
            "GET",
            "/api/v5/trade/fills",
            params={"instType": "SPOT"},
            auth=True,
            idempotent=True,
        )
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            trade_id = str(row.get("tradeId") or "")
            if not trade_id or trade_id in self._seen_trades:
                continue
            self._seen_trades.add(trade_id)
            self._new.append(_fill_from_row(row))

    def _orders_from(self, payload: dict[str, Any]) -> list[Order]:
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        orders: list[Order] = []
        for row in data:
            if isinstance(row, dict):
                cid = str(row.get("clOrdId") or "")
                purpose = self._purposes.get(cid, OrderPurpose.ENTRY)
                orders.append(parse_order_row({str(k): str(v) for k, v in row.items()}, purpose=purpose))
        return orders

    def _candles_from(self, inst_id: str, payload: dict[str, Any]) -> list[Candle]:
        ensure_ok(payload)
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        candles = [
            parse_okx_candle_row(inst_id, [str(cell) for cell in row])
            for row in data
            if isinstance(row, list)
        ]
        return sorted(candles, key=lambda candle: candle.ts)

    def _raise_on_item_error(self, payload: dict[str, Any], inst_id: str, client_order_id: str) -> None:
        ensure_ok(payload)
        item = _first_dict(payload)
        code = str(item.get("sCode") or "0")
        if code in {"0", ""}:
            return
        if code in _DUPLICATE_CLIENT_ID:
            existing = self.get_order(inst_id, client_order_id)
            if existing is not None:
                return
        raise OkxApiError(code, str(item.get("sMsg") or "order rejected"))

    def _refuse_if_unmocked(self) -> None:
        if os.environ.get(NETWORK_DISABLE_ENV) == "1" and not self.client.uses_mock_transport:
            raise RuntimeError("refusing to place an OKX order while the test network guard is on")


def _first_dict(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _fill_from_row(row: dict[str, Any]) -> Fill:
    from datetime import datetime
    from decimal import Decimal

    ts_raw = row.get("ts") or row.get("fillTime") or "0"
    fee = Decimal(str(row.get("fee") or "0"))
    return Fill(
        trade_id=str(row.get("tradeId")),
        client_order_id=str(row.get("clOrdId") or ""),
        inst_id=str(row.get("instId")),
        side=Side(str(row.get("side"))),
        price=Decimal(str(row.get("fillPx") or "0")),
        qty=Decimal(str(row.get("fillSz") or "0")),
        fee=abs(fee),
        fee_ccy=str(row.get("feeCcy") or "USDT"),
        ts=datetime.fromtimestamp(int(str(ts_raw)) / 1000, tz=UTC),
    )
