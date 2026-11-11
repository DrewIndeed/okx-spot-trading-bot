from __future__ import annotations

import json
from decimal import Decimal

import httpx

from okxspot.engine import build_engine
from okxspot.exchange.okx_client import OkxClient
from okxspot.exchange.okx_exchange import OkxExchange
from okxspot.exchange.paper import PaperExchange
from okxspot.models import OrderPurpose, OrderRequest, OrderType, Position, Side, Signal, Ticker
from okxspot.state import StateStore
from okxspot.strategy.base import BarContext
from tests.conftest import candle, day, instrument, make_settings

D = Decimal
CID = "c" + "a" * 31


class Flat:
    name = "flat"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return []


def test_software_stop_flattens_when_last_price_breaks() -> None:
    settings = make_settings(
        fees={
            "taker_fee": "0",
            "backtest_fee_multiplier": "1",
            "paper_fee_multiplier": "1",
            "slippage_bps": "0",
            "partial_fill_ratio": "1",
        }
    )
    inst = instrument(lot_sz=D("0.00001"), min_sz=D("0.00001"), tick_sz=D("0.1"), min_notional=D("1"))
    paper = PaperExchange(
        quote_ccy="USDT",
        cash=D("0"),
        taker_fee=D("0"),
        slippage_bps=D("0"),
        partial_fill_ratio=D("1"),
        instruments={"BTC-USDT": inst},
    )
    paper.bases["BTC-USDT"] = D("1")
    paper.set_ticker(Ticker("BTC-USDT", D("90"), D("91"), D("90"), day(1)))
    state = StateStore(":memory:")
    engine = build_engine(
        exchange=paper,
        settings=settings,
        strategy=Flat(),
        state=state,
        fee_rate=D("0"),
    )
    engine.book.positions["BTC-USDT"] = Position(
        "BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0)
    )
    engine._now = day(1)
    engine.enforce_software_stops()
    assert "BTC-USDT" not in engine.book.positions
    assert engine.book.realized_pnl == D("-10")
    state.close()


def test_ingest_latest_does_not_replay_a_processed_bar() -> None:
    settings = make_settings()
    inst = instrument()
    paper = PaperExchange(
        quote_ccy="USDT",
        cash=D("1000"),
        taker_fee=D("0"),
        slippage_bps=D("0"),
        partial_fill_ratio=D("1"),
        instruments={"BTC-USDT": inst},
    )
    seen: list[object] = []

    class Count:
        name = "count"

        def on_bar(self, ctx: BarContext) -> list[Signal]:
            seen.append(ctx.now)
            return []

    state = StateStore(":memory:")
    engine = build_engine(
        exchange=paper,
        settings=settings,
        strategy=Count(),
        state=state,
        fee_rate=D("0"),
    )
    series = {"BTC-USDT": [candle(0), candle(1), candle(2)]}
    engine.ingest_latest(series)
    engine.ingest_latest(series)
    assert seen == [day(2)]
    state.close()


def test_okx_adapter_public_and_private_routes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/public/time"):
            return httpx.Response(200, json={"code": "0", "data": [{"ts": "1700000000000"}]})
        if path.endswith("/public/instruments"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "instType": "SPOT",
                            "instId": "BTC-USDT",
                            "baseCcy": "BTC",
                            "quoteCcy": "USDT",
                            "tickSz": "0.1",
                            "lotSz": "0.00001",
                            "minSz": "0.00001",
                        }
                    ],
                },
            )
        if path.endswith("/market/candles"):
            return httpx.Response(
                200,
                json={"code": "0", "data": [["1700000000000", "100", "110", "90", "105", "1", "0", "0", "1"]]},
            )
        if path.endswith("/market/ticker"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT",
                            "bidPx": "100",
                            "askPx": "101",
                            "last": "100.5",
                            "ts": "1700000000000",
                        }
                    ],
                },
            )
        if path.endswith("/account/balance"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {"totalEq": "9000", "details": [{"ccy": "USDT", "availBal": "1000", "frozenBal": "0"}]}
                    ],
                },
            )
        if path.endswith("/account/config"):
            return httpx.Response(200, json={"code": "0", "data": [{"perm": "read_only,trade"}]})
        if path.endswith("/trade/order") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT",
                            "side": "buy",
                            "ordType": "market",
                            "sz": "0.01",
                            "clOrdId": CID,
                            "state": "live",
                            "accFillSz": "0",
                            "avgPx": "",
                            "ordId": "99",
                        }
                    ],
                },
            )
        if path.endswith("/trade/order") and request.method == "POST":
            body = json.loads(request.content.decode())
            assert body["tdMode"] == "cash"
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"ordId": "99", "clOrdId": CID, "sCode": "51016", "sMsg": "exists"}]},
            )
        if path.endswith("/trade/orders-pending"):
            return httpx.Response(200, json={"code": "0", "data": []})
        if path.endswith("/trade/order-algo") and request.method == "POST":
            body = json.loads(request.content.decode())
            assert body["tdMode"] == "cash"
            assert body["ordType"] == "conditional"
            return httpx.Response(200, json={"code": "0", "data": [{"algoId": "77", "sCode": "0"}]})
        if path.endswith("/trade/orders-algo-pending"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT",
                            "algoId": "77",
                            "algoClOrdId": CID,
                            "sz": "0.01",
                            "slTriggerPx": "90",
                            "state": "live",
                            "side": "sell",
                        }
                    ],
                },
            )
        if path.endswith("/trade/cancel-algos"):
            return httpx.Response(200, json={"code": "0", "data": []})
        if path.endswith("/trade/cancel-order"):
            return httpx.Response(200, json={"code": "0", "data": [{"sCode": "0"}]})
        if path.endswith("/trade/fills"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "tradeId": "fill-1",
                            "clOrdId": CID,
                            "instId": "BTC-USDT",
                            "side": "buy",
                            "fillPx": "100",
                            "fillSz": "0.01",
                            "fee": "-0.01",
                            "feeCcy": "USDT",
                            "ts": "1700000000000",
                        }
                    ],
                },
            )
        if path.endswith("/market/history-candles"):
            return httpx.Response(200, json={"code": "0", "data": []})
        return httpx.Response(404, json={"code": "404", "msg": path})

    client = OkxClient(
        api_key="test-key",
        secret="test-secret",
        passphrase="test-pass",
        transport=httpx.MockTransport(handler),
        limiter=_Limiter(),
    )
    exchange = OkxExchange(client, min_notional=D("1"))
    assert exchange.server_time_ms() == 1700000000000
    instrument_info = exchange.get_instrument("BTC-USDT")
    assert instrument_info.lot_sz == D("0.00001")
    assert exchange.get_instrument("BTC-USDT").lot_sz == D("0.00001")
    assert exchange.get_candles("BTC-USDT", "1D")[0].close == D("105")
    assert exchange.get_ticker("BTC-USDT").last == D("100.5")
    view = exchange.get_account_view()
    assert view.available_quote == D("1000")
    assert view.total_eq == D("9000")
    assert exchange.get_permissions() == {"read_only", "trade"}
    order = exchange.place_order(
        OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("0.01"), CID, purpose=OrderPurpose.ENTRY)
    )
    assert order.exchange_order_id == "99"
    assert exchange.get_open_orders() == []
    stop = exchange.place_algo_stop(
        __import__("okxspot.models", fromlist=["StopRequest"]).StopRequest("BTC-USDT", CID, D("0.01"), D("90"))
    )
    assert stop.algo_id == "77"
    assert exchange.get_open_algos("BTC-USDT")[0].algo_id == "77"
    exchange.cancel_algos("BTC-USDT")
    exchange.sync()
    fills = exchange.pop_new_fills()
    assert fills[0].qty == D("0.01")
    exchange.sync()
    assert exchange.pop_new_fills() == []
    client.close()


class _Limiter:
    def acquire(self) -> None:
        return None
