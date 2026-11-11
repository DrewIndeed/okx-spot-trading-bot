"""Shared data-layer and execution types. Kept narrow so Cypheous's upcoming data contract can adapt here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

# OKX candle row contract (matches /workspace/crypto/data.py parse):
# [ts_ms, open, high, low, close, volume_base, volume_quote, confirm]
CANDLE_FIELDS = ("ts", "open", "high", "low", "close", "volume_base", "volume_quote", "confirm")


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume_base: float = 0.0
    volume_quote: float = 0.0
    confirm: int = 1  # 1 = closed bar

    def as_row(self) -> list:
        return [self.ts, self.open, self.high, self.low, self.close,
                self.volume_base, self.volume_quote, self.confirm]

    @classmethod
    def from_row(cls, row: Sequence) -> "Candle":
        return cls(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume_base=float(row[5]) if len(row) > 5 else 0.0,
            volume_quote=float(row[6]) if len(row) > 6 else 0.0,
            confirm=int(row[7]) if len(row) > 7 else 1,
        )


@dataclass(frozen=True, slots=True)
class Ticker:
    inst_id: str
    last: float
    ts: int
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Signal:
    """Strategy output. signal_id is the idempotency key (duplicate signals are rejected)."""
    inst_id: str
    side: str  # "buy" | "sell" | "close"
    ts: int
    signal_id: str
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    strategy: str = ""
    strength: float = 1.0


@dataclass
class Position:
    inst_id: str
    qty: float
    avg_px: float
    stop: Optional[float] = None
    take_profit: Optional[float] = None
    signal_id: str = ""
    client_oid: str = ""
    opened_ts: int = 0
    # dollar risk at entry (qty * |entry - stop|), used for R multiples
    initial_risk: float = 0.0


@dataclass(frozen=True, slots=True)
class Fill:
    inst_id: str
    side: str  # "buy" | "sell"
    qty: float
    price: float  # fill price after slippage
    fee: float
    slippage: float  # price units of slippage applied
    notional: float
    ts: int
    client_oid: str
    signal_id: str
    reason: str = ""
    is_exit: bool = False
    pnl: float = 0.0  # realized net pnl on exits (after fees)
    r_multiple: Optional[float] = None


@dataclass
class OrderIntent:
    inst_id: str
    side: str
    qty: float
    price: float  # reference / intended price before slippage
    ts: int
    signal_id: str
    client_oid: str
    stop: Optional[float] = None
    take_profit: Optional[float] = None
    initial_risk: float = 0.0
    reason: str = ""
    is_exit: bool = False
    reduce_only: bool = False


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    qty: float = 0.0
    client_oid: str = ""
    initial_risk: float = 0.0
    extras: dict = field(default_factory=dict)
