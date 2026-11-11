from __future__ import annotations

from decimal import Decimal

from okxspot.models import AccountView


def sizing_equity(
    *,
    allocated_capital: Decimal,
    realized_pnl: Decimal,
    available_quote: Decimal,
) -> Decimal:
    """Equity used for position sizing.

    ``bot_equity = allocated_capital + realized_pnl``
    ``result = min(bot_equity, available_quote)``

    ``available_quote`` is free USDT (OKX ``availBal``) and is only a ceiling
    so the bot cannot spend cash it does not have. Total account equity is
    not an input and must not be passed in by renaming it to one of these.
    """
    if allocated_capital <= 0:
        raise ValueError("allocated_capital must be positive")
    if available_quote < 0:
        raise ValueError("available_quote cannot be negative")
    bot_equity = allocated_capital + realized_pnl
    if bot_equity < 0:
        bot_equity = Decimal("0")
    return min(bot_equity, available_quote)


def equity_from_view(
    allocated_capital: Decimal,
    realized_pnl: Decimal,
    view: AccountView,
) -> Decimal:
    """Sizer entry point. Reads ``available_quote`` and ignores ``total_eq``."""
    return sizing_equity(
        allocated_capital=allocated_capital,
        realized_pnl=realized_pnl,
        available_quote=view.available_quote,
    )
