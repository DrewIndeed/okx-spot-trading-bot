from __future__ import annotations

from decimal import Decimal

from okxspot.errors import StateError
from okxspot.models import ClosedTrade, Fill, Position, Side


class PortfolioBook:
    """Bot book. Realized PnL is the bot's own closed-trade result, not account equity."""

    def __init__(self, allocated_capital: Decimal) -> None:
        if allocated_capital <= 0:
            raise ValueError("allocated_capital must be positive")
        self.allocated_capital = allocated_capital
        self.realized_pnl = Decimal("0")
        self.positions: dict[str, Position] = {}
        self.applied: set[str] = set()

    def apply_fill(
        self,
        fill: Fill,
        *,
        stop_price: Decimal | None = None,
        initial_risk_quote: Decimal | None = None,
    ) -> ClosedTrade | None:
        if fill.trade_id in self.applied:
            return None
        self.applied.add(fill.trade_id)
        if fill.side is Side.BUY:
            self._apply_buy(fill, stop_price, initial_risk_quote)
            return None
        return self._apply_sell(fill)

    def marked_equity(self, marks: dict[str, Decimal]) -> Decimal:
        unrealized = Decimal("0")
        for inst, pos in self.positions.items():
            price = marks.get(inst, pos.avg_price)
            unrealized += (price - pos.avg_price) * pos.qty
        return self.allocated_capital + self.realized_pnl + unrealized

    def exposure_notional(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for inst, pos in self.positions.items():
            price = marks.get(inst, pos.avg_price)
            total += pos.qty * price
        return total

    def _apply_buy(
        self,
        fill: Fill,
        stop_price: Decimal | None,
        initial_risk_quote: Decimal | None,
    ) -> None:
        cost = fill.qty * fill.price + fill.fee
        pos = self.positions.get(fill.inst_id)
        if pos is None:
            avg = cost / fill.qty
            if stop_price is not None and avg > stop_price:
                risk = (avg - stop_price) * fill.qty
            else:
                risk = initial_risk_quote if initial_risk_quote is not None else Decimal("0")
            self.positions[fill.inst_id] = Position(
                inst_id=fill.inst_id,
                qty=fill.qty,
                avg_price=avg,
                stop_price=stop_price,
                initial_risk_quote=risk,
                opened_at=fill.ts,
            )
            return
        new_qty = pos.qty + fill.qty
        pos.avg_price = (pos.avg_price * pos.qty + cost) / new_qty
        pos.qty = new_qty
        if stop_price is not None:
            pos.stop_price = stop_price
        if pos.stop_price is not None and pos.avg_price > pos.stop_price:
            pos.initial_risk_quote = (pos.avg_price - pos.stop_price) * pos.qty

    def _apply_sell(self, fill: Fill) -> ClosedTrade | None:
        pos = self.positions.get(fill.inst_id)
        if pos is None:
            raise StateError(f"sell fill {fill.trade_id} has no position in {fill.inst_id}")
        if fill.qty > pos.qty:
            raise StateError(
                f"sell qty {fill.qty} exceeds position {pos.qty} for {fill.inst_id}"
            )
        proceeds = fill.qty * fill.price - fill.fee
        pnl = proceeds - pos.avg_price * fill.qty
        self.realized_pnl += pnl
        pos.realized_fragment += pnl
        entry_ts = pos.opened_at
        trade_pnl = pos.realized_fragment
        initial_risk = pos.initial_risk_quote
        risk_closed = initial_risk * (fill.qty / pos.qty) if pos.qty > 0 else initial_risk
        pos.qty -= fill.qty
        pos.initial_risk_quote = initial_risk - risk_closed
        if pos.qty > 0:
            return None
        del self.positions[fill.inst_id]
        return ClosedTrade(
            inst_id=fill.inst_id,
            pnl=trade_pnl,
            initial_risk_quote=initial_risk,
            entry_ts=entry_ts,
            exit_ts=fill.ts,
        )
