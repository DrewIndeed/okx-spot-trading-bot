"""Data-layer interface. Designed to be easy to adapt when Cypheous's contract arrives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

import numpy as np

from tradingbot.types import Candle, Ticker


@dataclass
class CandleArrays:
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume_base: np.ndarray
    volume_quote: np.ndarray
    confirm: np.ndarray

    def __len__(self) -> int:
        return int(len(self.ts))


def candles_to_arrays(candles: Sequence[Candle]) -> CandleArrays:
    return CandleArrays(
        ts=np.array([c.ts for c in candles], dtype=np.int64),
        open=np.array([c.open for c in candles], float),
        high=np.array([c.high for c in candles], float),
        low=np.array([c.low for c in candles], float),
        close=np.array([c.close for c in candles], float),
        volume_base=np.array([c.volume_base for c in candles], float),
        volume_quote=np.array([c.volume_quote for c in candles], float),
        confirm=np.array([c.confirm for c in candles], dtype=np.int8),
    )


class MarketDataFeed(Protocol):
    """Public market-data contract (candles + ticker). Implementations: REST cache, WS, later Cypheous."""

    def get_candles(
        self,
        inst_id: str,
        bar: str,
        *,
        days: Optional[int] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        update: bool = True,
    ) -> list[Candle]:
        ...

    def get_ticker(self, inst_id: str) -> Optional[Ticker]:
        ...
