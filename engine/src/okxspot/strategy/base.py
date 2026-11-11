from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from okxspot.models import Candle, Position, Signal


@dataclass(frozen=True, slots=True)
class BarContext:
    """Closed-bar view. ``history`` ends at the current bar and includes no future bars."""

    inst_id: str
    history: tuple[Candle, ...]
    regime_history: tuple[Candle, ...]
    position: Position | None
    now: datetime


class Strategy(Protocol):
    name: str

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        """Return entry or exit signals for this closed bar. Do not place orders."""
