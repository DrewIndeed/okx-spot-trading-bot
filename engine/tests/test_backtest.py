from __future__ import annotations

from decimal import Decimal

from okxspot.backtest.report import write_backtest_report
from okxspot.backtest.session import run_session
from okxspot.engine import build_engine
from okxspot.exchange.paper import PaperExchange
from okxspot.models import Position, Side, Signal
from okxspot.state import StateStore
from okxspot.strategy.base import BarContext
from tests.conftest import candle, day, instrument, make_settings

D = Decimal


class Script:
    name = "script"

    def __init__(self, stop: str, exit_on: int | None = None) -> None:
        self.stop = D(stop)
        self.exit_on = exit_on
        self.seen: list[object] = []

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        assert all(bar.ts <= ctx.now for bar in ctx.history)
        assert all(bar.ts <= ctx.now for bar in ctx.regime_history)
        self.seen.append(ctx.now)
        if ctx.now == day(0) and ctx.position is None:
            return [Signal("BTC-USDT", Side.BUY, self.stop, "entry")]
        if self.exit_on is not None and ctx.position is not None and ctx.now == day(self.exit_on):
            return [Signal("BTC-USDT", Side.SELL, None, "exit")]
        return []


def _flat_fees() -> dict[str, str]:
    return {
        "taker_fee": "0",
        "backtest_fee_multiplier": "1",
        "paper_fee_multiplier": "1",
        "slippage_bps": "0",
        "partial_fill_ratio": "1",
    }


def test_entry_fills_on_next_bar_open() -> None:
    settings = make_settings(fees=_flat_fees())
    candles = {
        "BTC-USDT": [
            candle(0, "100"),
            candle(1, "100", open_="110", high="110", low="100", close="110"),
        ]
    }
    result = run_session(candles, settings, fee_rate=D("0"), strategy=Script("90"))
    position = result.engine.book.positions["BTC-USDT"]
    assert position.qty == D("1")
    assert position.avg_price == D("110")
    assert "in_sample" in result.report
    assert "out_of_sample" in result.report
    assert "profitable" in str(result.report["disclaimer"])


def test_fees_reduce_round_trip() -> None:
    candles = {
        "BTC-USDT": [
            candle(0, "100"),
            candle(1, "100"),
            candle(2, "100"),
            candle(3, "100", open_="110", high="110", low="100", close="110"),
        ]
    }
    free = run_session(
        candles,
        make_settings(fees=_flat_fees()),
        fee_rate=D("0"),
        strategy=Script("50", exit_on=2),
    )
    paid = run_session(
        candles,
        make_settings(fees=_flat_fees()),
        fee_rate=D("0.001"),
        strategy=Script("50", exit_on=2),
    )
    assert free.engine.trades[0].pnl == D("2")
    assert paid.engine.trades[0].pnl < free.engine.trades[0].pnl
    assert paid.report["full"]["n_trades"] == 1  # type: ignore[index]


def test_report_png(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(fees=_flat_fees())
    candles = {"BTC-USDT": [candle(i, "100") for i in range(6)]}
    result = run_session(candles, settings, fee_rate=D("0"), strategy=Script("50", exit_on=3))
    json_path, png_path = write_backtest_report(result.report, tmp_path)
    assert json_path.exists()
    assert png_path.read_bytes().startswith(b"\x89PNG")


class Flat:
    name = "flat"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return []


def test_kill_switch_flattens_open_position() -> None:
    settings = make_settings(fees=_flat_fees())
    inst = instrument(
        lot_sz=D("0.00001"),
        min_sz=D("0.00001"),
        tick_sz=D("0.1"),
        min_notional=D("1"),
    )
    paper = PaperExchange(
        quote_ccy="USDT",
        cash=D("0"),
        taker_fee=D("0"),
        slippage_bps=D("0"),
        partial_fill_ratio=D("1"),
        instruments={"BTC-USDT": inst},
    )
    paper.bases["BTC-USDT"] = D("10")
    state = StateStore(":memory:")
    engine = build_engine(
        exchange=paper,
        settings=settings,
        strategy=Flat(),
        state=state,
        fee_rate=D("0"),
    )
    engine.book.positions["BTC-USDT"] = Position(
        "BTC-USDT",
        D("10"),
        D("100"),
        D("50"),
        D("500"),
        day(0),
    )
    first = candle(0, "100", low="99")
    engine.process_bars({"BTC-USDT": first}, {"BTC-USDT": (first,)})
    assert engine.risk.halted is False
    crash = candle(1, "80", open_="100", high="100", low="79", close="80")
    engine.process_bars({"BTC-USDT": crash}, {"BTC-USDT": (first, crash)})
    assert engine.risk.halted
    assert "BTC-USDT" not in engine.book.positions
    assert engine.book.realized_pnl == D("-200")
    state.close()
