from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from okxspot.models import ClosedTrade


@dataclass(frozen=True, slots=True)
class Metrics:
    cagr: Decimal | None
    max_drawdown: Decimal
    profit_factor: Decimal | None
    expectancy_r: Decimal | None
    win_rate: Decimal | None
    n_trades: int
    exposure: Decimal
    start_equity: Decimal | None
    end_equity: Decimal | None
    days: Decimal


def max_drawdown(equity: list[Decimal]) -> Decimal:
    if not equity:
        return Decimal("0")
    peak = equity[0]
    worst = Decimal("0")
    for value in equity:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def cagr(start: Decimal, end: Decimal, days: Decimal) -> Decimal | None:
    if start <= 0 or end <= 0 or days <= 0:
        return None
    years = float(days) / 365.25
    if years <= 0:
        return None
    growth = float(end / start) ** (1.0 / years) - 1.0
    return Decimal(str(growth))


def profit_factor(trades: list[ClosedTrade]) -> Decimal | None:
    gross_profit = sum((trade.pnl for trade in trades if trade.pnl > 0), start=Decimal("0"))
    gross_loss = sum((-trade.pnl for trade in trades if trade.pnl < 0), start=Decimal("0"))
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def expectancy_r(trades: list[ClosedTrade]) -> Decimal | None:
    if not trades:
        return None
    total = sum((trade.r_multiple for trade in trades), start=Decimal("0"))
    return total / Decimal(len(trades))


def win_rate(trades: list[ClosedTrade]) -> Decimal | None:
    if not trades:
        return None
    wins = sum(1 for trade in trades if trade.won)
    return Decimal(wins) / Decimal(len(trades))


def exposure_ratio(exposed_bars: int, total_bars: int) -> Decimal:
    if total_bars <= 0:
        return Decimal("0")
    return Decimal(exposed_bars) / Decimal(total_bars)


def period_days(start: datetime | None, end: datetime | None) -> Decimal:
    if start is None or end is None or end <= start:
        return Decimal("0")
    return Decimal(str((end - start).total_seconds() / 86400))


def compute_metrics(
    equity: list[tuple[datetime, Decimal]],
    trades: list[ClosedTrade],
    *,
    exposed_bars: int,
    total_bars: int,
) -> Metrics:
    values = [point[1] for point in equity]
    start_equity = values[0] if values else None
    end_equity = values[-1] if values else None
    start_ts = equity[0][0] if equity else None
    end_ts = equity[-1][0] if equity else None
    days = period_days(start_ts, end_ts)
    grown = (
        cagr(start_equity, end_equity, days)
        if start_equity is not None and end_equity is not None
        else None
    )
    return Metrics(
        cagr=grown,
        max_drawdown=max_drawdown(values),
        profit_factor=profit_factor(trades),
        expectancy_r=expectancy_r(trades),
        win_rate=win_rate(trades),
        n_trades=len(trades),
        exposure=exposure_ratio(exposed_bars, total_bars),
        start_equity=start_equity,
        end_equity=end_equity,
        days=days,
    )


def slice_curve(
    equity: list[tuple[datetime, Decimal]],
    start_index: int,
    end_index: int,
) -> list[tuple[datetime, Decimal]]:
    return equity[start_index:end_index]


def trades_exiting_between(
    trades: list[ClosedTrade],
    start: datetime | None,
    end: datetime | None,
    *,
    include_start: bool,
) -> list[ClosedTrade]:
    chosen: list[ClosedTrade] = []
    for trade in trades:
        if start is not None:
            if include_start:
                if trade.exit_ts < start:
                    continue
            elif trade.exit_ts <= start:
                continue
        if end is not None and trade.exit_ts > end:
            continue
        chosen.append(trade)
    return chosen
