"""Risk manager: fixed-fraction sizing, caps, kill-switch, position limit, duplicate-order guard.

All dollar equity used for sizing comes from the paper book, which is initialized at
Config.allocated_capital (dedicated sub-account allocation). This module has no path
to an OKX account balance and must never be given one.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from tradingbot.config import RiskConfig
from tradingbot.portfolio import Portfolio
from tradingbot.types import RiskDecision, Signal


def make_client_oid(signal_id: str) -> str:
    h = hashlib.sha256(signal_id.encode()).hexdigest()[:20]
    return f"tb{h}"  # 22 chars, within OKX 32-char clOrdId limit


def _round_lot(qty: float, lot_size: float) -> float:
    if lot_size <= 0:
        return qty
    # floor to lot; generous epsilon so 5.0 is not floored to 4.99999
    n = int((qty / lot_size) + 1e-9)
    return round(n * lot_size, 12)


class RiskManager:
    def __init__(self, cfg: RiskConfig, allocated_capital: float):
        if allocated_capital is None or allocated_capital <= 0:
            raise ValueError(
                "RiskManager requires allocated_capital > 0; "
                "never size from the main OKX account balance."
            )
        self.cfg = cfg
        self.allocated_capital = float(allocated_capital)
        self.risk_pct = cfg.clamped_risk_pct()

    def paper_equity(self, book: Portfolio, marks: Optional[dict[str, float]] = None) -> float:
        """Paper equity only (cash + MTM). Origin: allocated_capital ± paper P&L. Not account NAV."""
        return book.equity(marks)

    def evaluate(
        self,
        signal: Signal,
        book: Portfolio,
        price: float,
        *,
        marks: Optional[dict[str, float]] = None,
        qty_hint: Optional[float] = None,
        weight_hint: Optional[float] = None,
    ) -> RiskDecision:
        """Approve or reject a desired entry/exit.

        qty_hint / weight_hint: if a strategy (e.g. D1) already computed a size as a
        fraction of allocated/paper equity, we start from that and still apply caps,
        lot rounding, kill-switch, and the duplicate-order guard.
        """
        cfg = self.cfg
        oid = make_client_oid(signal.signal_id)
        marks = marks or {}

        if signal.side == "close" or (signal.side == "sell"):
            # Exits: still reject duplicate client oids / signal ids, but skip entry caps.
            if book.has_signal(signal.signal_id) or book.has_oid(oid):
                return RiskDecision(False, "duplicate_signal_or_oid", client_oid=oid)
            pos = book.positions.get(signal.inst_id)
            if not pos or pos.qty <= 0:
                return RiskDecision(False, "no_position_to_exit", client_oid=oid)
            return RiskDecision(True, "exit_ok", qty=pos.qty, client_oid=oid, initial_risk=pos.initial_risk)

        if book.halted:
            return RiskDecision(False, f"halted:{book.halt_reason}", client_oid=oid)
        if book.has_signal(signal.signal_id):
            return RiskDecision(False, "duplicate_signal", client_oid=oid)
        if book.has_oid(oid):
            return RiskDecision(False, "duplicate_client_oid", client_oid=oid)

        equity = self.paper_equity(book, marks)
        if equity <= 0:
            return RiskDecision(False, "equity_non_positive", client_oid=oid)

        # Kill-switch: max drawdown from paper peak
        peak = max(book.equity_peak, equity)
        if peak > 0 and (peak - equity) / peak >= cfg.max_drawdown:
            book.halted = True
            book.halt_reason = "max_drawdown"
            return RiskDecision(False, "kill_switch_max_drawdown", client_oid=oid)

        # Daily loss cap vs start-of-day paper equity
        day0 = book.day_start_equity or equity
        if day0 > 0 and (day0 - equity) / day0 >= cfg.daily_loss_cap:
            book.halted = True
            book.halt_reason = "daily_loss_cap"
            return RiskDecision(False, "daily_loss_cap", client_oid=oid)

        if book.open_position_count() >= cfg.max_open_positions and signal.inst_id not in book.positions:
            return RiskDecision(False, "max_open_positions", client_oid=oid)

        if price <= 0:
            return RiskDecision(False, "invalid_price", client_oid=oid)

        stop = signal.stop_price
        # Size: prefer strategy weight, else fixed-fraction of paper equity
        if qty_hint is not None and qty_hint > 0:
            qty = float(qty_hint)
            initial_risk = (qty * (price - stop)) if stop and stop < price else 0.0
        elif weight_hint is not None and weight_hint > 0:
            qty = (equity * float(weight_hint)) / price
            initial_risk = (qty * (price - stop)) if stop and stop < price else equity * float(weight_hint)
        else:
            if not stop or stop >= price:
                return RiskDecision(False, "invalid_stop", client_oid=oid)
            risk_per_unit = price - stop
            risk_dollars = equity * self.risk_pct
            qty = risk_dollars / risk_per_unit
            initial_risk = risk_dollars

        # Exposure caps (vs paper equity, never account balance)
        asset_notional = book.notional(signal.inst_id, marks)
        total_notional = book.total_notional(marks)
        room_asset = max(0.0, cfg.max_exposure_asset * equity - asset_notional)
        room_total = max(0.0, cfg.max_exposure_total * equity - total_notional)
        room = min(room_asset, room_total, equity)  # cannot spend more than cash; cash checked below
        if room <= 0:
            return RiskDecision(False, "exposure_cap", client_oid=oid)
        qty = min(qty, room / price)
        # cash constraint
        qty = min(qty, max(0.0, book.cash / price))
        qty = _round_lot(qty, cfg.lot_size)
        if qty <= 0:
            return RiskDecision(False, "lot_rounded_to_zero", client_oid=oid)
        notional = qty * price
        if notional < cfg.min_notional:
            return RiskDecision(False, "min_notional", client_oid=oid, qty=qty)

        if stop and stop < price:
            initial_risk = qty * (price - stop)

        return RiskDecision(
            True,
            "ok",
            qty=qty,
            client_oid=oid,
            initial_risk=initial_risk,
            extras={"notional": notional, "equity": equity, "allocated_capital": self.allocated_capital},
        )

    def refresh_halts(self, book: Portfolio, marks: Optional[dict[str, float]] = None) -> Optional[str]:
        """Update peaks and trip kill-switch / daily cap on mark-to-market (no new order)."""
        equity = self.paper_equity(book, marks)
        if book.equity_peak > 0 and (book.equity_peak - equity) / book.equity_peak >= self.cfg.max_drawdown:
            book.halted = True
            book.halt_reason = "max_drawdown"
            return book.halt_reason
        day0 = book.day_start_equity or equity
        if day0 > 0 and (day0 - equity) / day0 >= self.cfg.daily_loss_cap:
            book.halted = True
            book.halt_reason = "daily_loss_cap"
            return book.halt_reason
        return None
