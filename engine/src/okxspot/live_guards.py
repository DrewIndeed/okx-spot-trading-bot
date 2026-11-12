"""Live NO-GO guards (Sofia's e95875d addendum).

Guard 1 — fill window. OKX ``GET /api/v5/trade/fills`` only returns the last
3 days (``before=<billId>`` pages newer records). If the last committed fill
sync is older than ``fill_window_hours - fill_safety_margin_hours`` the bot
backfills from ``GET /api/v5/trade/fills-history`` (3 months, same paging) or,
when that is not possible, refuses and trips the kill switch. A non-empty
ledger with no recorded sync time is refused: the gap is unknowable.

Guard 2 — position mismatch. On each reconcile the exchange balance of every
traded base coin must equal the bot ledger within a per-asset tolerance. The
quote coin is shortfall-only: the venue must hold at least the bot's own cash
(allocated capital + realized PnL - open cost). Quote above that, such as the
user's non-bot funds, is never treated as drift and never used for sizing.
A mismatch trips the kill switch (cancel open orders, no auto-flatten from an
untrusted book) and needs an operator re-arm.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from okxspot.errors import LiveGuardError

if TYPE_CHECKING:
    from okxspot.config import GuardSettings
    from okxspot.portfolio import PortfolioBook

FILL_SYNC_TS_KEY = "last_fill_sync_ts"
FILL_GAP_PREFIX = "fill_gap"
POSITION_MISMATCH_PREFIX = "position_mismatch"
UNTRUSTED_LEDGER_PREFIXES = (FILL_GAP_PREFIX, POSITION_MISMATCH_PREFIX)


class FillGapError(LiveGuardError):
    """Fills may be missing from the book; trading must not resume."""


def _hours(value: Decimal) -> timedelta:
    return timedelta(seconds=float(value * 3600))


def fill_sync_decision(
    *,
    last_sync: datetime | None,
    now: datetime,
    ledger_nonempty: bool,
    history_supported: bool,
    settings: GuardSettings,
) -> tuple[str, str]:
    """Return ``("fills" | "history" | "refuse", reason)``."""
    if last_sync is None:
        if ledger_nonempty:
            return "refuse", f"{FILL_GAP_PREFIX}:missing_last_fill_sync_ts_on_nonempty_ledger"
        return "fills", "fresh_start"
    age = now - last_sync
    if age < _hours(settings.fill_window_hours - settings.fill_safety_margin_hours):
        return "fills", "within_fills_window"
    if not settings.fill_history_backfill or not history_supported:
        return "refuse", f"{FILL_GAP_PREFIX}:last_sync_age={age} exceeds /trade/fills window; no backfill"
    if age >= _hours(settings.fill_history_window_hours - settings.fill_history_margin_hours):
        return "refuse", f"{FILL_GAP_PREFIX}:last_sync_age={age} exceeds /trade/fills-history window"
    return "history", f"last_sync_age={age}; backfill via /trade/fills-history"


def bot_quote_cash(book: PortfolioBook) -> Decimal:
    cost = sum((pos.qty * pos.avg_price for pos in book.positions.values()), Decimal("0"))
    return book.allocated_capital + book.realized_pnl - cost


def position_mismatches(
    book: PortfolioBook,
    balances: dict[str, Decimal],
    *,
    symbols: list[str],
    quote_ccy: str,
    settings: GuardSettings,
) -> list[str]:
    """Describe every traded asset whose venue balance disagrees with the ledger."""
    out: list[str] = []
    bases = sorted({s.split("-")[0].upper() for s in symbols} | {i.split("-")[0].upper() for i in book.positions})
    for base in bases:
        ledger = sum(
            (p.qty for inst, p in book.positions.items() if inst.split("-")[0].upper() == base), Decimal("0")
        )
        venue = balances.get(base, Decimal("0"))
        tol = settings.position_tolerance.get(base, settings.position_default_tolerance)
        if abs(venue - ledger) > tol:
            out.append(f"{base} ledger={ledger} exchange={venue} tol={tol}")
    expected = bot_quote_cash(book)
    venue_quote = balances.get(quote_ccy.upper(), Decimal("0"))
    if venue_quote < expected - settings.quote_shortfall_tolerance:
        out.append(
            f"{quote_ccy.upper()} shortfall exchange={venue_quote} bot_cash={expected} "
            f"tol={settings.quote_shortfall_tolerance}"
        )
    return out


def is_untrusted_ledger_reason(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(UNTRUSTED_LEDGER_PREFIXES)
