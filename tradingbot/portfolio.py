"""Portfolio / state persisted to JSON. Survives process restart (load on start, atomic save).

Equity is cash + mark-to-market of positions. Initial cash is Config.allocated_capital
(fixed dedicated sub-account allocation). Never derived from the main OKX account balance.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from tradingbot.types import Fill, Position


@dataclass
class Portfolio:
    allocated_capital: float
    cash: float
    equity_peak: float
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: Optional[str] = None
    positions: dict[str, Position] = field(default_factory=dict)
    used_client_oids: list[str] = field(default_factory=list)
    used_signal_ids: list[str] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    path: Optional[str] = None
    _id_cap: int = 10_000

    @classmethod
    def new(cls, allocated_capital: float, path: Optional[str] = None, day: str = "") -> "Portfolio":
        cap = float(allocated_capital)
        if cap <= 0:
            raise ValueError("allocated_capital must be positive")
        return cls(
            allocated_capital=cap,
            cash=cap,
            equity_peak=cap,
            day=day,
            day_start_equity=cap,
            path=path,
        )

    def equity(self, marks: Optional[dict[str, float]] = None) -> float:
        total = self.cash
        for inst, pos in self.positions.items():
            px = (marks or {}).get(inst, pos.avg_px)
            total += pos.qty * px
        return total

    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.qty > 0)

    def notional(self, inst_id: str, marks: Optional[dict[str, float]] = None) -> float:
        pos = self.positions.get(inst_id)
        if not pos or pos.qty <= 0:
            return 0.0
        px = (marks or {}).get(inst_id, pos.avg_px)
        return pos.qty * px

    def total_notional(self, marks: Optional[dict[str, float]] = None) -> float:
        return sum(self.notional(i, marks) for i in self.positions)

    def remember_ids(self, signal_id: str, client_oid: str) -> None:
        if signal_id and signal_id not in self.used_signal_ids:
            self.used_signal_ids.append(signal_id)
        if client_oid and client_oid not in self.used_client_oids:
            self.used_client_oids.append(client_oid)
        if len(self.used_signal_ids) > self._id_cap:
            self.used_signal_ids = self.used_signal_ids[-self._id_cap:]
        if len(self.used_client_oids) > self._id_cap:
            self.used_client_oids = self.used_client_oids[-self._id_cap:]

    def has_signal(self, signal_id: str) -> bool:
        return bool(signal_id) and signal_id in self.used_signal_ids

    def has_oid(self, client_oid: str) -> bool:
        return bool(client_oid) and client_oid in self.used_client_oids

    def apply_fill(self, fill: Fill, *, stop=None, take_profit=None, initial_risk: float = 0.0) -> None:
        if fill.side == "buy" and not fill.is_exit:
            cost = fill.notional + fill.fee
            self.cash -= cost
            prev = self.positions.get(fill.inst_id)
            if prev and prev.qty > 0:
                new_qty = prev.qty + fill.qty
                prev.avg_px = (prev.avg_px * prev.qty + fill.price * fill.qty) / new_qty
                prev.qty = new_qty
                if stop is not None:
                    prev.stop = stop
            else:
                self.positions[fill.inst_id] = Position(
                    inst_id=fill.inst_id,
                    qty=fill.qty,
                    avg_px=fill.price,
                    stop=stop,
                    take_profit=take_profit,
                    signal_id=fill.signal_id,
                    client_oid=fill.client_oid,
                    opened_ts=fill.ts,
                    initial_risk=initial_risk or ((fill.price - (stop or 0)) * fill.qty if stop else 0.0),
                )
        else:
            # sell / exit (spot: sell reduces position, credits cash)
            proceeds = fill.notional - fill.fee
            self.cash += proceeds
            prev = self.positions.get(fill.inst_id)
            if prev:
                prev.qty = max(0.0, prev.qty - fill.qty)
                if prev.qty == 0:
                    del self.positions[fill.inst_id]
        self.remember_ids(fill.signal_id, fill.client_oid)
        self.fills.append(asdict(fill) if hasattr(fill, "__dataclass_fields__") else dict(fill))  # type: ignore
        # fills store dataclass as dict
        from dataclasses import is_dataclass
        if is_dataclass(fill) and not isinstance(fill, type):
            self.fills[-1] = asdict(fill)

    def update_peaks(self, equity: float, day: str) -> None:
        if day and day != self.day:
            self.day = day
            self.day_start_equity = equity
        if equity > self.equity_peak:
            self.equity_peak = equity

    def to_dict(self) -> dict:
        return {
            "allocated_capital": self.allocated_capital,
            "cash": self.cash,
            "equity_peak": self.equity_peak,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "used_client_oids": list(self.used_client_oids),
            "used_signal_ids": list(self.used_signal_ids),
            "fills": list(self.fills),
        }

    @classmethod
    def from_dict(cls, d: dict, path: Optional[str] = None) -> "Portfolio":
        if "allocated_capital" not in d or d["allocated_capital"] is None:
            raise ValueError(
                "state is missing allocated_capital; refuse to infer equity from any account balance"
            )
        positions = {}
        for k, v in (d.get("positions") or {}).items():
            positions[k] = Position(**v)
        return cls(
            allocated_capital=float(d["allocated_capital"]),
            cash=float(d["cash"]),
            equity_peak=float(d.get("equity_peak") or d["cash"]),
            day=str(d.get("day") or ""),
            day_start_equity=float(d.get("day_start_equity") or d.get("allocated_capital") or 0.0),
            halted=bool(d.get("halted") or False),
            halt_reason=d.get("halt_reason"),
            positions=positions,
            used_client_oids=list(d.get("used_client_oids") or []),
            used_signal_ids=list(d.get("used_signal_ids") or []),
            fills=list(d.get("fills") or []),
            path=path,
        )

    def save(self, path: Optional[str] = None) -> Path:
        p = Path(path or self.path or "state/portfolio.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        tmp.replace(p)
        return p

    @classmethod
    def load(cls, path: str, *, allocated_capital: Optional[float] = None) -> "Portfolio":
        """Load persisted state. If the file does not exist, start a new book at allocated_capital."""
        p = Path(path)
        if not p.exists():
            if allocated_capital is None:
                raise ValueError("allocated_capital is required to create a new portfolio")
            return cls.new(allocated_capital, path=path)
        data = json.loads(p.read_text())
        port = cls.from_dict(data, path=str(p))
        return port
