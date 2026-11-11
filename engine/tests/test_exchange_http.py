from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from okxspot.errors import AmbiguousOrderError, LiveGuardError, OkxApiError
from okxspot.exchange.okx_client import OkxClient
from okxspot.exchange.okx_exchange import OkxExchange
from okxspot.exchange.okx_parse import (
    parse_account_view,
    parse_instrument,
    parse_okx_candle_row,
    parse_permissions,
)
from okxspot.exchange.signing import (
    build_request_path,
    clock_offset_ms,
    format_okx_timestamp,
    okx_sign,
)
from okxspot.exchange.ws import parse_public_message
from okxspot.models import OrderStatus

SECRET = "22582BD0CFF14C41EDBF1AB98506286D"
STAMP = "2020-12-08T09:08:57.715Z"
KNOWN_SIGN = "Ku79U+75wKPSP3i+t02ssUto76AYiAT/aws7hI3GZpg="


def test_signing_vector_and_timestamp() -> None:
    path = "/api/v5/account/balance?ccy=USDT"
    assert okx_sign(SECRET, STAMP, "GET", path, "") == KNOWN_SIGN
    assert build_request_path("/api/v5/account/balance", {"ccy": "USDT"}) == path
    assert format_okx_timestamp(1000) == "1970-01-01T00:00:01.000Z"
    assert clock_offset_ms(1000, 5000) == 4000


def test_signed_header_matches_vector() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sign"] = request.headers["OK-ACCESS-SIGN"]
        seen["stamp"] = request.headers["OK-ACCESS-TIMESTAMP"]
        seen["key"] = request.headers["OK-ACCESS-KEY"]
        return httpx.Response(200, json={"code": "0", "data": []})

    client = OkxClient(
        api_key="test-key",
        secret=SECRET,
        passphrase="test-pass",
        transport=httpx.MockTransport(handler),
        limiter=_no_limit(),
    )
    client.timestamp = lambda: STAMP  # type: ignore[method-assign]
    client.request(
        "GET",
        "/api/v5/account/balance",
        params={"ccy": "USDT"},
        auth=True,
        idempotent=True,
    )
    assert seen["stamp"] == STAMP
    assert seen["sign"] == KNOWN_SIGN
    assert seen["key"] == "test-key"
    client.close()


def test_clock_sync_offset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "data": [{"ts": "1005000"}]})

    client = OkxClient(
        transport=httpx.MockTransport(handler),
        now_ms=lambda: 1_000_000,
        limiter=_no_limit(),
        sleeper=lambda _s: None,
    )
    assert client.sync_clock() == 5000
    client.close()


def test_retry_idempotent_get_and_not_post() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="bad")
        return httpx.Response(200, json={"code": "0", "data": []})

    client = OkxClient(
        transport=httpx.MockTransport(handler),
        max_retries=5,
        limiter=_no_limit(),
        sleeper=lambda _s: None,
    )
    client.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
    assert calls["n"] == 3

    calls["n"] = 0

    def post(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="bad")

    poster = OkxClient(
        transport=httpx.MockTransport(post),
        max_retries=5,
        limiter=_no_limit(),
        sleeper=lambda _s: None,
    )
    with pytest.raises(AmbiguousOrderError):
        poster.request(
            "POST",
            "/api/v5/trade/order",
            body={"instId": "BTC-USDT"},
            auth=False,
            idempotent=False,
        )
    assert calls["n"] == 1
    client.close()
    poster.close()


def test_network_guard_and_missing_keys() -> None:
    bare = OkxClient(limiter=_no_limit())
    with pytest.raises(RuntimeError, match="network disabled"):
        bare.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
    bare.close()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": "0"}))
    mocked = OkxClient(transport=transport, limiter=_no_limit())
    with pytest.raises(LiveGuardError):
        mocked.request("GET", "/api/v5/account/balance", auth=True, idempotent=True)
    mocked.close()


def test_parsers() -> None:
    candle = parse_okx_candle_row(
        "BTC-USDT",
        ["1704067200000", "100", "110", "90", "105", "3", "0", "0", "1"],
    )
    assert candle.close == Decimal("105")
    assert candle.confirmed
    perms = parse_permissions({"data": [{"perm": "read_only,trade,withdraw"}]})
    assert perms == {"read_only", "trade", "withdraw"}
    view = parse_account_view(
        {
            "data": [
                {
                    "totalEq": "50000",
                    "details": [{"ccy": "USDT", "availBal": "1000", "frozenBal": "0"}],
                }
            ]
        },
        "USDT",
    )
    assert view.available_quote == Decimal("1000")
    assert view.total_eq == Decimal("50000")
    with pytest.raises(ValueError):
        parse_instrument(
            {
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "baseCcy": "BTC",
                "quoteCcy": "USDT",
                "tickSz": "0.1",
                "lotSz": "1",
                "minSz": "1",
            },
            Decimal("1"),
        )


def test_place_order_is_spot_cash_and_sized_in_base() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json

            bodies.append(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"ordId": "1", "clOrdId": "c" + "a" * 31, "sCode": "0"}]},
            )
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
                        "clOrdId": "c" + "a" * 31,
                        "state": "filled",
                        "accFillSz": "0.01",
                        "avgPx": "100",
                        "fee": "-0.01",
                        "feeCcy": "USDT",
                        "ordId": "1",
                    }
                ],
            },
        )

    client = OkxClient(
        api_key="test-key",
        secret="test-secret",
        passphrase="test-pass",
        transport=httpx.MockTransport(handler),
        limiter=_no_limit(),
    )
    exchange = OkxExchange(client, min_notional=Decimal("1"))
    from okxspot.models import OrderPurpose, OrderRequest, OrderType, Side

    order = exchange.place_order(
        OrderRequest(
            "BTC-USDT",
            Side.BUY,
            OrderType.MARKET,
            Decimal("0.01"),
            "c" + "a" * 31,
            purpose=OrderPurpose.ENTRY,
        )
    )
    assert order.status is OrderStatus.FILLED
    assert bodies[0]["tdMode"] == "cash"
    assert bodies[0]["tgtCcy"] == "base_ccy"
    assert "SWAP" not in str(bodies[0])
    client.close()


def test_business_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": "51000", "msg": "parameter error"})

    client = OkxClient(transport=httpx.MockTransport(handler), limiter=_no_limit(), sleeper=lambda _s: None)
    with pytest.raises(OkxApiError) as caught:
        client.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
    assert caught.value.code == "51000"
    assert calls["n"] == 1
    client.close()


def test_ws_parse_candle_and_ticker() -> None:
    import json

    raw = json.dumps(
        {
            "arg": {"channel": "candle1D", "instId": "BTC-USDT"},
            "data": [["1704067200000", "100", "110", "90", "105", "3", "0", "0", "1"]],
        }
    )
    events = parse_public_message(raw)
    assert events[0].candle is not None
    assert events[0].candle.close == Decimal("105")
    ticker = json.dumps(
        {
            "arg": {"channel": "tickers", "instId": "ETH-USDT"},
            "data": [{"instId": "ETH-USDT", "bidPx": "10", "askPx": "11", "last": "10.5", "ts": "1704067200000"}],
        }
    )
    parsed = parse_public_message(ticker)
    assert parsed[0].ticker is not None
    assert parsed[0].ticker.ask == Decimal("11")
    assert parse_public_message(json.dumps({"event": "subscribe"})) == []


class _Limiter:
    def acquire(self) -> None:
        return None


def _no_limit() -> _Limiter:
    return _Limiter()
