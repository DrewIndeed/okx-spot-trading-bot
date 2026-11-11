"""Pluggable strategy interface. Cypheous's spec will implement this; the engine does not hard-code an edge."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence

from tradingbot.types import Candle, Position, Signal


class Strategy(Protocol):
    name: str

    def warmup_bars(self) -> int: ...

    def on_candles(
        self,
        inst_id: str,
        candles: Sequence[Candle],
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        """Called with the full confirmed history up to and including the signal bar.

        Return a Signal or None. Implementations must be causal (no look-ahead).
        """
        ...
