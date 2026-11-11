from __future__ import annotations

import json
import ssl

from okxspot.marketdata import PublicFeed, is_ws_fallback_error
from okxspot.models import Candle
from tests.conftest import candle


class Rest:
    def __init__(self) -> None:
        self.calls = 0

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        self.calls += 1
        return [candle(0, inst=inst_id)]


class _Sock:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self.messages


class _CM:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages

    def __enter__(self) -> _Sock:
        return _Sock(self.messages)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def test_ssl_eof_falls_back_to_rest_and_stays_there() -> None:
    connects = {"n": 0}

    def connect(url: str) -> object:
        connects["n"] += 1
        raise ssl.SSLError("EOF occurred in violation of protocol")

    rest = Rest()
    feed = PublicFeed(
        rest,
        ws_url="wss://ws.okx.com:8443/ws/v5/public",
        symbols=["BTC-USDT"],
        bar="1D",
        connect=connect,
    )
    first = feed.next_candles()
    assert feed.transport == "rest"
    assert "EOF" in (feed.fallback_reason or "")
    assert first["BTC-USDT"][0].inst_id == "BTC-USDT"
    feed.fallback_reason = "still-rest"
    second = feed.next_candles()
    assert second["BTC-USDT"]
    assert connects["n"] == 1
    assert rest.calls == 2
    assert is_ws_fallback_error(ssl.SSLError("EOF occurred in violation of protocol"))


def test_healthy_websocket_does_not_poll_rest() -> None:
    payload = json.dumps(
        {
            "arg": {"channel": "candle1D", "instId": "BTC-USDT"},
            "data": [["1704067200000", "100", "110", "90", "105", "3", "0", "0", "1"]],
        }
    )

    def connect(url: str) -> _CM:
        return _CM([payload])

    rest = Rest()
    feed = PublicFeed(rest, ws_url="wss://example", symbols=["BTC-USDT"], bar="1D", connect=connect)
    candles = feed.next_candles()
    assert feed.transport == "websocket"
    assert rest.calls == 0
    assert candles["BTC-USDT"][0].close == candles["BTC-USDT"][0].close
