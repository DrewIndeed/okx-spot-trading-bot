from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from okxspot.config import RiskSettings
from okxspot.killswitch import KillSwitch
from okxspot.decimals import dec_str, parse_decimal
from okxspot.models import Instrument, OrderPurpose, Side
from okxspot.risk.equity import sizing_equity
from okxspot.risk.rounding import meets_min_notional, round_price, round_qty


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    qty: Decimal
    stop_price: Decimal | None
    reason: str
    risk_amount: Decimal
    sizing_equity_used: Decimal


class RiskManager:
    """Fixed-fraction risk, exposure caps, and the drawdown kill-switch.

    Sizing uses :func:`sizing_equity` only. There is no parameter for total
    account balance.
    """

    def __init__(
        self,
        cfg: RiskSettings,
        *,
        on_flatten: Callable[[str], None] | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_flatten = on_flatten or (lambda _reason: None)
        self.kill_switch = kill_switch
        self._submit_times: list[datetime] = []
        self.halted = False
        self.halt_reason: str | None = None
        self.peak = Decimal("0")
        self.day: date | None = None
        self.day_start = Decimal("0")
        self.daily_loss_hit = False
        self.cooldown_remaining = 0
        self.flatten_sent = False
        self.bar_index = 0

    def on_new_bar(self) -> None:
        """Call once at the start of each closed bar, before new entries."""
        self.bar_index += 1
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

    def register_close(self, pnl: Decimal) -> None:
        if pnl < 0 and self.cfg.cooldown_bars_after_loss > 0:
            self.cooldown_remaining = self.cfg.cooldown_bars_after_loss

    def on_equity(self, equity: Decimal, ts: datetime) -> None:
        """Update drawdown and the daily loss flag from bot marked equity.

        ``equity`` must be the bot book (allocated capital + realized + unrealized),
        never the exchange account total.
        """
        if ts.tzinfo is None:
            raise ValueError("equity timestamp must be timezone-aware")
        if not self.halted and equity > self.peak:
            self.peak = equity
        day = ts.astimezone(UTC).date()
        if self.day != day:
            self.day = day
            self.day_start = equity
            self.daily_loss_hit = False
        if self.day_start > 0:
            loss_pct = (self.day_start - equity) / self.day_start
            self.daily_loss_hit = loss_pct >= self.cfg.daily_loss_limit_pct
        if self.halted or self.peak <= 0:
            return
        drawdown = (self.peak - equity) / self.peak
        if drawdown >= self.cfg.max_drawdown_pct:
            self.halted = True
            self.halt_reason = "max_drawdown"
            if not self.flatten_sent:
                self.flatten_sent = True
                self._on_flatten("max_drawdown")


    def pretrade(
        self,
        *,
        inst_id: str,
        side: Side,
        purpose: OrderPurpose,
        notional: Decimal,
        now: datetime,
        td_mode: str = "cash",
        leverage: Decimal | None = None,
        record: bool = True,
    ) -> RiskDecision:
        """Single pre-trade gate used before any submit.

        Sizing equity is never read from an exchange account balance.
        """
        eq = Decimal("0")

        def reject(reason: str) -> RiskDecision:
            return RiskDecision(False, Decimal("0"), None, reason, Decimal("0"), eq)

        if now.tzinfo is None:
            raise ValueError("pretrade timestamp must be timezone-aware")
        if self.kill_switch is not None and self.kill_switch.is_tripped:
            return reject("kill_switch")
        if self.halted and purpose in {OrderPurpose.ENTRY}:
            return reject("halted")
        if purpose is OrderPurpose.ENTRY and self.daily_loss_hit:
            return reject("daily_loss_limit")
        if purpose is OrderPurpose.ENTRY and side is Side.SELL:
            return reject("spot_long_only")
        if (td_mode or "cash").lower() != "cash":
            return reject("no_margin")
        if leverage is not None and leverage > 1:
            return reject("no_leverage")
        inst_up = inst_id.upper()
        if "SWAP" in inst_up or "FUTURES" in inst_up or inst_id.count("-") != 1:
            return reject("spot_only")
        min_n = self.cfg.min_notional
        max_n = self.cfg.max_notional
        if purpose is OrderPurpose.ENTRY and min_n is not None and notional < min_n:
            return reject("min_notional")
        if purpose is OrderPurpose.ENTRY and max_n is not None and notional > max_n:
            return reject("max_notional")
        day = now.astimezone(UTC).date()
        minute_ago = now.timestamp() - 60
        minute_count = sum(1 for ts in self._submit_times if ts.timestamp() >= minute_ago)
        day_count = sum(1 for ts in self._submit_times if ts.astimezone(UTC).date() == day)
        if minute_count >= self.cfg.max_orders_per_minute:
            return reject("max_orders_per_minute")
        if day_count >= self.cfg.max_orders_per_day:
            return reject("max_orders_per_day")
        if record:
            self._submit_times.append(now)
        return RiskDecision(True, Decimal("0"), None, "ok", Decimal("0"), eq)

    def evaluate_entry(
        self,
        *,
        entry_price: Decimal,
        stop_price: Decimal | None,
        allocated_capital: Decimal,
        realized_pnl: Decimal,
        available_quote: Decimal,
        open_positions: int,
        current_exposure_notional: Decimal,
        instrument: Instrument,
        has_position: bool,
        fee_rate: Decimal,
    ) -> RiskDecision:
        eq = sizing_equity(
            allocated_capital=allocated_capital,
            realized_pnl=realized_pnl,
            available_quote=available_quote,
        )

        def reject(reason: str) -> RiskDecision:
            return RiskDecision(False, Decimal("0"), stop_price, reason, Decimal("0"), eq)

        if self.kill_switch is not None and self.kill_switch.is_tripped:
            return reject("kill_switch")
        if self.halted:
            return reject("halted")
        if self.daily_loss_hit:
            return reject("daily_loss_limit")
        if self.cooldown_remaining > 0:
            return reject("loss_cooldown")
        if has_position:
            return reject("position_open")
        if open_positions >= self.cfg.max_concurrent_positions:
            return reject("max_concurrent_positions")
        if eq <= 0:
            return reject("no_sizing_equity")
        if entry_price <= 0 or stop_price is None or stop_price <= 0 or stop_price >= entry_price:
            return reject("invalid_stop")
        if current_exposure_notional < 0:
            return reject("invalid_exposure")

        risk_budget = eq * self.cfg.risk_per_trade
        rounded_stop = round_price(stop_price, instrument)
        distance = entry_price - rounded_stop
        if distance <= 0 or rounded_stop <= 0:
            return reject("invalid_stop")
        qty = risk_budget / distance

        max_coin_notional = self.cfg.per_coin_cap_pct * eq
        qty = min(qty, max_coin_notional / entry_price)

        room = self.cfg.max_total_exposure_pct * eq - current_exposure_notional
        if room <= 0:
            return reject("max_total_exposure")
        qty = min(qty, room / entry_price)

        unit_cost = entry_price * (Decimal("1") + fee_rate)
        if unit_cost <= 0:
            return reject("invalid_price")
        qty = min(qty, eq / unit_cost)

        # Tick rounding can widen the stop. Resize so actual risk stays in budget.
        distance = entry_price - rounded_stop
        qty = min(qty, risk_budget / distance)
        qty = round_qty(qty, instrument)
        if qty <= 0:
            return reject("below_lot_size")
        if not meets_min_notional(qty, entry_price, instrument):
            return reject("min_notional")
        actual_risk = qty * (entry_price - rounded_stop)
        return RiskDecision(True, qty, rounded_stop, "ok", actual_risk, eq)

    def export_state(self) -> dict[str, str]:
        return {
            "halted": "1" if self.halted else "0",
            "halt_reason": self.halt_reason or "",
            "peak": dec_str(self.peak) or "0",
            "day": self.day.isoformat() if self.day else "",
            "day_start": dec_str(self.day_start) or "0",
            "daily_loss_hit": "1" if self.daily_loss_hit else "0",
            "cooldown_remaining": str(self.cooldown_remaining),
            "flatten_sent": "1" if self.flatten_sent else "0",
            "bar_index": str(self.bar_index),
        }

    def load_state(self, data: dict[str, str]) -> None:
        self.halted = data.get("halted") == "1"
        reason = data.get("halt_reason") or ""
        self.halt_reason = reason or None
        self.peak = parse_decimal(data.get("peak") or "0")
        day = data.get("day") or ""
        self.day = date.fromisoformat(day) if day else None
        self.day_start = parse_decimal(data.get("day_start") or "0")
        self.daily_loss_hit = data.get("daily_loss_hit") == "1"
        self.cooldown_remaining = int(data.get("cooldown_remaining") or "0")
        self.flatten_sent = data.get("flatten_sent") == "1"
        self.bar_index = int(data.get("bar_index") or "0")

    def export_json(self) -> str:
        return json.dumps(self.export_state())

    def load_json(self, raw: str) -> None:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("risk state must be an object")
        self.load_state({str(k): str(v) for k, v in loaded.items()})
