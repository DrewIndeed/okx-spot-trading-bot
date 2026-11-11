from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Protocol, cast

from okxspot.exchange.okx_parse import parse_okx_candle_row, parse_ticker_row
from okxspot.models import Candle, Ticker
from okxspot.safety import NETWORK_DISABLE_ENV


class WsEvent:
    def __init__(self, kind: str, candle: Candle | None = None, ticker: Ticker | None = None) -> None:
        self.kind = kind
        self.candle = candle
        self.ticker = ticker


class Socket(Protocol):
    def send(self, data: str) -> None: ...

    def __iter__(self) -> Iterator[str]: ...


class Connect(Protocol):
    def __call__(self, url: str) -> SocketContext: ...


class SocketContext(Protocol):
    def __enter__(self) -> Socket: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool: ...


def parse_public_message(raw: str) -> list[WsEvent]:
    """Parse one OKX public websocket frame into candles or tickers."""
    msg = json.loads(raw)
    if not isinstance(msg, dict) or msg.get("event"):
        return []
    arg = msg.get("arg") or {}
    if not isinstance(arg, dict):
        return []
    channel = str(arg.get("channel") or "")
    inst = str(arg.get("instId") or "")
    data = msg.get("data") or []
    if not isinstance(data, list) or not inst:
        return []
    events: list[WsEvent] = []
    if channel.startswith("candle"):
        for row in data:
            if isinstance(row, list):
                events.append(WsEvent("candle", candle=parse_okx_candle_row(inst, [str(x) for x in row])))
    elif channel == "tickers":
        for row in data:
            if isinstance(row, dict):
                events.append(WsEvent("ticker", ticker=parse_ticker_row({str(k): str(v) for k, v in row.items()})))
    return events


def default_connect(url: str) -> SocketContext:
    if os.environ.get(NETWORK_DISABLE_ENV) == "1":
        raise RuntimeError("network disabled (OKXSPOT_DISABLE_NETWORK=1)")
    import websockets

    return cast(SocketContext, websockets.connect(url))


def run_public_stream(
    url: str,
    args: list[dict[str, str]],
    handler: Callable[[WsEvent], None],
    *,
    connect: Callable[[str], SocketContext],
    sleep: Callable[[float], None],
    stop: Callable[[], bool],
    max_failures: int | None = None,
) -> None:
    """Subscribe and dispatch until ``stop`` or the socket ends cleanly.

    Disconnects back off from 1s to 30s. ``max_failures`` is for tests.
    """
    delay = 1.0
    failures = 0
    while not stop():
        try:
            with connect(url) as ws:
                ws.send(json.dumps({"op": "subscribe", "args": args}))
                for raw in ws:
                    if stop():
                        return
                    for event in parse_public_message(raw):
                        handler(event)
                return
        except Exception:
            failures += 1
            if max_failures is not None and failures >= max_failures:
                raise
            if stop():
                return
            sleep(delay)
            delay = min(delay * 2, 30.0)
