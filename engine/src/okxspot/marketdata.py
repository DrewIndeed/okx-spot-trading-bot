"""Public market data. WebSocket first, REST polling if the socket cannot stay up.

Some hosts see ``ssl.SSLEOFError`` / "EOF occurred in violation of protocol"
against OKX public websocket endpoints. After that class of failure the runner
stops retrying the socket and polls REST candles and tickers for the rest of
the process.
"""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable
from typing import Protocol

from okxspot.exchange.ws import WsEvent, run_public_stream
from okxspot.models import Candle

log = logging.getLogger("okxspot.marketdata")


class WsFallbackError(RuntimeError):
    """Public websocket failed; the caller should poll REST."""


class RestCandles(Protocol):
    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]: ...


def is_ws_fallback_error(exc: BaseException) -> bool:
    """True for SSL EOF and other transport failures that should switch to REST."""
    transport = (
        ssl.SSLError,
        EOFError,
        ConnectionError,
        TimeoutError,
        ConnectionResetError,
        BrokenPipeError,
    )
    if isinstance(exc, transport):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    markers = ("eof", "ssl", "connection reset", "connection aborted", "timed out", "timeout")
    return any(marker in text for marker in markers)


class PublicFeed:
    """One session of public data for the paper/live runner.

    ``transport`` is ``websocket`` until a transport failure, then ``rest``.
    """

    def __init__(
        self,
        rest: RestCandles,
        *,
        ws_url: str,
        symbols: list[str],
        bar: str,
        connect: Callable[..., object],
        sleep: Callable[[float], None] | None = None,
        limit: int = 300,
    ) -> None:
        self.rest = rest
        self.ws_url = ws_url
        self.symbols = list(symbols)
        self.bar = bar
        self._connect = connect
        self._sleep = sleep or (lambda _seconds: None)
        self.limit = limit
        self.transport = "websocket"
        self.fallback_reason: str | None = None

    def next_candles(self, timeout: float | None = None) -> dict[str, list[Candle]]:
        if self.transport == "websocket":
            try:
                candles = self._read_ws(timeout=timeout)
            except WsFallbackError as exc:
                self._fall_back(str(exc))
            else:
                # A timeout with only unconfirmed (or no) bars must not block the
                # trading loop and must not drop a healthy socket to REST.
                return candles
        return self._read_rest()

    def _fall_back(self, reason: str) -> None:
        self.transport = "rest"
        self.fallback_reason = reason
        log.warning("websocket failed (%s); falling back to REST polling", reason)

    def _read_rest(self) -> dict[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        for symbol in self.symbols:
            candles = [bar for bar in self.rest.get_candles(symbol, self.bar, self.limit) if bar.confirmed]
            out[symbol] = candles
        return out

    def _read_ws(self, timeout: float | None = None) -> dict[str, list[Candle]]:
        collected: list[WsEvent] = []
        deadline = time.monotonic() + (timeout if timeout is not None else 5.0)

        def stop() -> bool:
            if time.monotonic() >= deadline:
                return True
            insts = {event.candle.inst_id for event in collected if event.candle is not None}
            return len(insts) >= len(self.symbols)

        def handler(event: WsEvent) -> None:
            if event.kind == "candle" and event.candle is not None and event.candle.confirmed:
                collected.append(event)

        args = [{"channel": f"candle{self.bar}", "instId": symbol} for symbol in self.symbols]
        try:
            run_public_stream(
                self.ws_url,
                args,
                handler,
                connect=self._connect,  # type: ignore[arg-type]
                sleep=self._sleep,
                stop=stop,
                max_failures=1,
                deadline=deadline,
            )
        except Exception as exc:
            if is_ws_fallback_error(exc):
                raise WsFallbackError(str(exc)) from exc
            raise
        grouped: dict[str, list[Candle]] = {symbol: [] for symbol in self.symbols}
        for event in collected:
            if event.candle is None:
                continue
            grouped.setdefault(event.candle.inst_id, []).append(event.candle)
        return grouped
