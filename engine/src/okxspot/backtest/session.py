from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from okxspot.alerts import Alerter
from okxspot.backtest.report import build_report
from okxspot.config import Settings
from okxspot.engine import TradingEngine, build_engine
from okxspot.exchange.paper import PaperExchange
from okxspot.instruments import default_instruments
from okxspot.models import Candle, Instrument
from okxspot.state import StateStore
from okxspot.strategy.base import Strategy
from okxspot.strategy.donchian import DonchianAtrTrend


@dataclass
class SessionResult:
    engine: TradingEngine
    report: dict[str, object]


def build_strategy(settings: Settings) -> DonchianAtrTrend:
    spec = settings.strategy
    return DonchianAtrTrend(
        donchian_n=spec.donchian_n,
        atr_n=spec.atr_n,
        atr_mult=spec.atr_mult,
        regime_sma=spec.regime_sma,
    )


def run_session(
    candles: dict[str, list[Candle]],
    settings: Settings,
    *,
    fee_rate: Decimal,
    strategy: Strategy | None = None,
    instruments: dict[str, Instrument] | None = None,
    state_path: str | Path = ":memory:",
    alerter: Alerter | None = None,
    total_eq_decoy: Decimal | None = None,
    starting_cash: Decimal | None = None,
    immediate: bool = False,
) -> SessionResult:
    """Replay closed bars. Orders signaled on bar T fill at bar T+1 open."""
    chosen = strategy or build_strategy(settings)
    book_instruments = instruments or default_instruments(
        list(dict.fromkeys([*settings.symbols, settings.regime_symbol])),
        settings.okx.min_notional_usdt,
    )
    series: dict[str, list[Candle]] = {}
    for inst, rows in candles.items():
        closed = [bar for bar in rows if bar.confirmed]
        closed.sort(key=lambda bar: bar.ts)
        for index in range(1, len(closed)):
            if closed[index].ts <= closed[index - 1].ts:
                raise ValueError(f"candles for {inst} must be strictly increasing")
        series[inst] = closed
    state = StateStore(state_path)
    paper = PaperExchange(
        quote_ccy=settings.quote_ccy,
        cash=settings.allocated_capital if starting_cash is None else starting_cash,
        taker_fee=fee_rate,
        slippage_bps=settings.fees.slippage_bps,
        partial_fill_ratio=settings.fees.partial_fill_ratio,
        instruments=book_instruments,
        immediate=immediate,
        total_eq_decoy=total_eq_decoy,
    )
    engine = build_engine(
        exchange=paper,
        settings=settings,
        strategy=chosen,
        state=state,
        fee_rate=fee_rate,
        alerter=alerter,
    )
    timestamps = sorted({bar.ts for rows in series.values() for bar in rows})
    for ts in timestamps:
        histories: dict[str, tuple[Candle, ...]] = {}
        bars = {}
        for inst, rows in series.items():
            history = tuple(bar for bar in rows if bar.ts <= ts)
            histories[inst] = history
            if history and history[-1].ts == ts and inst in settings.symbols:
                bars[inst] = history[-1]
        if bars:
            engine.process_bars(bars, histories)
    report = build_report(
        equity_curve=engine.equity_curve,
        trades=engine.trades,
        exposed_bars=engine.exposed_bars,
        total_bars=engine.total_bars,
        oos_ratio=settings.backtest.oos_ratio,
        fee_rate=fee_rate,
        slippage_bps=settings.fees.slippage_bps,
        allocated_capital=settings.allocated_capital,
        strategy_name=getattr(chosen, "name", "strategy"),
    )
    return SessionResult(engine=engine, report=report)
