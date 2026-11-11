"""Paper / dry-run order manager. Simulates fills with fee + slippage. No live order path."""

from __future__ import annotations

from tradingbot.config import LiveTradingRefused
from tradingbot.portfolio import Portfolio
from tradingbot.types import Fill, OrderIntent


class OrderManager:
    def __init__(self, fee_taker: float = 0.001, slippage: float = 0.002):
        self.fee_taker = float(fee_taker)
        self.slippage = float(slippage)

    def simulate_fill(self, intent: OrderIntent) -> Fill:
        """Apply slippage then taker fee. Long-only spot: buy lifts the price, sell hits the bid."""
        if intent.qty <= 0 or intent.price <= 0:
            raise ValueError("invalid intent qty/price")
        if intent.side == "buy":
            fill_px = intent.price * (1.0 + self.slippage)
        elif intent.side in ("sell", "close"):
            fill_px = intent.price * (1.0 - self.slippage)
            intent.side = "sell"
        else:
            raise ValueError(f"unsupported side {intent.side}")
        notional = fill_px * intent.qty
        fee = notional * self.fee_taker
        slip_abs = abs(fill_px - intent.price)
        pnl = 0.0
        r_mult = None
        if intent.is_exit:
            # pnl filled in by apply_exit using avg_px; here we only price the fill
            pass
        return Fill(
            inst_id=intent.inst_id,
            side=intent.side,
            qty=intent.qty,
            price=fill_px,
            fee=fee,
            slippage=slip_abs,
            notional=notional,
            ts=intent.ts,
            client_oid=intent.client_oid,
            signal_id=intent.signal_id,
            reason=intent.reason,
            is_exit=intent.is_exit,
            pnl=pnl,
            r_multiple=r_mult,
        )

    def submit_paper(self, book: Portfolio, intent: OrderIntent) -> Fill:
        fill = self.simulate_fill(intent)
        pnl = 0.0
        r_mult = None
        if intent.is_exit or intent.side == "sell":
            pos = book.positions.get(intent.inst_id)
            if pos and pos.qty > 0:
                # net proceeds vs cost basis, minus this sell fee (entry fee already in avg? 
                # avg_px is fill price; entry fee was paid from cash and is in realized via cash).
                # Dollar PnL ≈ qty*(sell_px - avg_px) - sell_fee. Entry fee already reduced cash.
                pnl = intent.qty * (fill.price - pos.avg_px) - fill.fee
                if pos.initial_risk and pos.initial_risk > 0:
                    r_mult = pnl / pos.initial_risk
        fill = Fill(
            inst_id=fill.inst_id,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            slippage=fill.slippage,
            notional=fill.notional,
            ts=fill.ts,
            client_oid=fill.client_oid,
            signal_id=fill.signal_id,
            reason=fill.reason,
            is_exit=bool(intent.is_exit or intent.side == "sell"),
            pnl=pnl,
            r_multiple=r_mult,
        )
        book.apply_fill(
            fill,
            stop=intent.stop,
            take_profit=intent.take_profit,
            initial_risk=intent.initial_risk,
        )
        return fill

    def submit_live(self, *args, **kwargs):
        raise LiveTradingRefused("Live order path is not implemented. Refusing.")
