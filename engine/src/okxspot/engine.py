from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from decimal import Decimal

from okxspot.alerts import Alerter, LogAlerter
from okxspot.config import Settings
from okxspot.exchange.base import Exchange
from okxspot.exchange.paper import PaperExchange
from okxspot.killswitch import KillSwitch
from okxspot.models import (
    Candle,
    ClosedTrade,
    OrderPurpose,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Signal,
)
from okxspot.orders.ids import make_client_order_id
from okxspot.orders.journal import OrderJournal
from okxspot.orders.manager import OrderManager
from okxspot.risk.manager import RiskManager
from okxspot.safety import extra_funds_warning
from okxspot.stale import StaleGuard
from okxspot.state import StateStore
from okxspot.strategy.base import BarContext, Strategy


class TradingEngine:
    """One closed-bar step: fills, stops, risk, then new signals.

    Signals generated on a bar rest until the next bar's open. Kill-switch
    and software-stop exits are filled immediately at the mark.
    """

    def __init__(
        self,
        *,
        exchange: Exchange,
        strategy: Strategy,
        risk: RiskManager,
        orders: OrderManager,
        book: object,
        alerter: Alerter,
        settings: Settings,
        state: StateStore,
        fee_rate: Decimal,
        kill_switch: KillSwitch | None = None,
        stale: StaleGuard | None = None,
    ) -> None:
        from okxspot.portfolio import PortfolioBook

        if not isinstance(book, PortfolioBook):
            raise TypeError("book must be a PortfolioBook")
        self.exchange = exchange
        self.strategy = strategy
        self.risk = risk
        self.orders = orders
        self.book = book
        self.alerter = alerter
        self.settings = settings
        self.state = state
        self.fee_rate = fee_rate
        self.kill_switch = kill_switch
        self.stale = stale
        self.marks: dict[str, Decimal] = {}
        self._entry_meta: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        self.equity_curve: list[tuple[datetime, Decimal]] = []
        self.trades: list[ClosedTrade] = []
        self.exposed_bars = 0
        self.total_bars = 0
        self._now: datetime | None = None
        self._cancel_done = False
        self.untrusted_price = False
        self._untrusted_stop = False

    def flatten_client_order_id(self, pos: Position, purpose: OrderPurpose = OrderPurpose.FLATTEN) -> str:
        token = "flatten" if purpose is OrderPurpose.FLATTEN else "softstop"
        return make_client_order_id(token, pos.inst_id, "sell", int(pos.opened_at.timestamp()), purpose=token)

    def apply_kill_switch(self) -> None:
        if self.kill_switch is None:
            return
        self.kill_switch.reload()
        if not self.kill_switch.is_tripped:
            self._cancel_done = False
            return
        if self.kill_switch.cancel_open and not self._cancel_done:
            for inst in {pos.inst_id for pos in self.book.positions.values()} | set(self.settings.symbols):
                self.exchange.cancel_open_orders(inst)
            self._cancel_done = True

    def process_bars(
        self,
        bars: dict[str, Candle],
        histories: dict[str, tuple[Candle, ...]],
    ) -> None:
        if not bars:
            return
        self.risk.on_new_bar()
        self.total_bars += 1
        ts = max(bar.ts for bar in bars.values())
        self._now = ts
        for bar in bars.values():
            self.exchange.fill_resting(bar)
        self._drain()
        self._ensure_stops()
        for bar in bars.values():
            self.exchange.trigger_stops(bar)
        self._drain()
        for inst, bar in bars.items():
            self.marks[inst] = bar.close
        equity = self.book.marked_equity(self.marks)
        if self.book.positions:
            self.exposed_bars += 1
        self.equity_curve.append((ts, equity))
        self.risk.on_equity(equity, ts)
        if self.risk.halted or (self.kill_switch is not None and self.kill_switch.is_tripped):
            if self.book.positions:
                reason = self.risk.halt_reason or (self.kill_switch.reason if self.kill_switch else "halt")
                self.force_flatten(reason)
            self._mark_processed(bars)
            self._persist()
            return
        view = self.exchange.get_account_view()
        bot_equity = self.book.allocated_capital + self.book.realized_pnl
        warning = extra_funds_warning(view.total_eq, bot_equity)
        if warning:
            self.alerter.send("warning", warning)
        for inst, bar in bars.items():
            if inst not in self.settings.symbols:
                continue
            history = histories.get(inst, (bar,))
            regime = histories.get(self.settings.regime_symbol, ())
            ctx = BarContext(
                inst_id=inst,
                history=history,
                regime_history=regime,
                position=self.book.positions.get(inst),
                now=bar.ts,
            )
            for signal in self.strategy.on_bar(ctx):
                self._handle_signal(signal, bar)
        self._mark_processed(bars)
        self._persist()

    def ingest_latest(self, series: dict[str, list[Candle]]) -> None:
        """Process only the newest confirmed bar of each symbol.

        Full history is still passed into the strategy so long lookbacks work.
        Already-processed timestamps are skipped so a restart cannot duplicate them.
        """
        pending: dict[datetime, dict[str, Candle]] = {}
        confirmed: dict[str, list[Candle]] = {}
        for inst, candles in series.items():
            closed = [bar for bar in candles if bar.confirmed]
            confirmed[inst] = closed
            if inst not in self.settings.symbols or not closed:
                continue
            last = closed[-1]
            if self._done(inst, last.ts):
                continue
            pending.setdefault(last.ts, {})[inst] = last
        for ts in sorted(pending):
            histories = {
                inst: tuple(bar for bar in bars if bar.ts <= ts)
                for inst, bars in confirmed.items()
            }
            self.process_bars(pending[ts], histories)

    def enforce_software_stops(self) -> None:
        """Backup spot stop. Used when the public socket is down and on live polls.

        If an exchange algo is still working and the last price has not traded
        through the stop, it is left alone. A breach cancels the algo and sells.
        A ticker failure must not be treated as "price is still above the stop".
        """
        for inst, pos in list(self.book.positions.items()):
            if pos.stop_price is None or pos.qty <= 0:
                continue
            try:
                last = self.exchange.get_ticker(inst).last
            except Exception:
                self.untrusted_price = True
                self._untrusted_stop = True
                self.alerter.send(
                    "critical",
                    f"ticker failed for {inst}; fail-closed software stop",
                    event="stale_data",
                )
                last = pos.stop_price
            if last > pos.stop_price:
                continue
            self.exchange.cancel_algos(inst)
            self._exit_position(inst, pos.qty, "softstop", last, OrderPurpose.STOP, opened_at=pos.opened_at)

    def force_flatten(self, reason: str) -> None:
        self.alerter.send("critical", f"kill-switch flatten: {reason}")
        for inst, pos in list(self.book.positions.items()):
            if pos.qty <= 0:
                continue
            self.exchange.cancel_algos(inst)
            self.exchange.cancel_open_orders(inst)
            mark = self.marks.get(inst, pos.avg_price)
            self._exit_position(inst, pos.qty, "flat", mark, OrderPurpose.FLATTEN, opened_at=pos.opened_at)
        if getattr(self, "_crash_before_persist", False):
            raise RuntimeError("injected crash before persist")
        self._persist()

    def sync_exchange_fills(self) -> None:
        loader = getattr(self.exchange, "load_fill_cursor", None)
        if callable(loader):
            loader(self.state.get_kv("fill_cursor"))
        self.exchange.sync()
        self._drain()

    def _handle_signal(self, signal: Signal, bar: Candle) -> None:
        if self.kill_switch is not None and self.kill_switch.is_tripped:
            return
        if signal.side is Side.SELL:
            pos = self.book.positions.get(signal.inst_id)
            if pos is None or pos.qty <= 0:
                return
            cid = make_client_order_id("exit", signal.inst_id, "sell", int(bar.ts.timestamp()))
            self.orders.submit(
                OrderRequest(
                    inst_id=signal.inst_id,
                    side=Side.SELL,
                    ord_type=OrderType.MARKET,
                    qty=pos.qty,
                    client_order_id=cid,
                    purpose=OrderPurpose.EXIT,
                ),
                now=bar.ts,
            )
            return
        view = self.exchange.get_account_view()
        worst = None
        try:
            ticker = self.exchange.get_ticker(signal.inst_id)
            worst = max(ticker.ask, ticker.last)
        except Exception:
            slip = self.settings.fees.slippage_bps / Decimal("10000")
            worst = bar.close * (Decimal("1") + slip) if slip > 0 else bar.close
        decision = self.risk.evaluate_entry(
            entry_price=bar.close,
            stop_price=signal.stop_price,
            allocated_capital=self.book.allocated_capital,
            realized_pnl=self.book.realized_pnl,
            available_quote=view.available_quote,
            open_positions=len(self.book.positions),
            current_exposure_notional=self.book.exposure_notional(self.marks),
            instrument=self.exchange.get_instrument(signal.inst_id),
            has_position=signal.inst_id in self.book.positions,
            fee_rate=self.fee_rate,
            worst_case_price=worst,
        )
        if not decision.allowed or decision.qty <= 0 or decision.stop_price is None:
            return
        cid = make_client_order_id("entry", signal.inst_id, "buy", int(bar.ts.timestamp()))
        self._entry_meta[cid] = (decision.stop_price, decision.risk_amount)
        self.state.save_meta(cid, decision.stop_price, decision.risk_amount)
        self.orders.submit(
            OrderRequest(
                inst_id=signal.inst_id,
                side=Side.BUY,
                ord_type=OrderType.MARKET,
                qty=decision.qty,
                client_order_id=cid,
                purpose=OrderPurpose.ENTRY,
            ),
            now=bar.ts,
        )
        self.alerter.send(
            "info",
            f"entry {signal.inst_id} qty={decision.qty} stop={decision.stop_price}",
        )

    def _exit_position(
        self,
        inst: str,
        qty: Decimal,
        purpose: str,
        price: Decimal,
        order_purpose: OrderPurpose,
        *,
        opened_at: datetime | None = None,
    ) -> None:
        if qty <= 0:
            return
        pos = self.book.positions.get(inst)
        opened = opened_at or (pos.opened_at if pos is not None else None)
        if opened is not None and order_purpose in {OrderPurpose.FLATTEN, OrderPurpose.STOP}:
            stand_in = pos or Position(inst, qty, price, None, Decimal("0"), opened)
            cid = self.flatten_client_order_id(stand_in, order_purpose)
        else:
            bar_ts = int((self._now or opened or datetime.now(UTC)).timestamp())
            cid = make_client_order_id(purpose, inst, "sell", bar_ts, purpose=purpose)
        existing = self.state.get_order(cid) or self.exchange.get_order(inst, cid)
        if existing is not None and existing.status not in {OrderStatus.REJECTED, OrderStatus.CANCELED}:
            # Same durable id already lives on the venue or in the journal.
            # A partial fill must not trigger a second full-qty sell.
            if existing.filled_qty >= qty or existing.status is OrderStatus.PARTIALLY_FILLED:
                return
            if existing.status in {OrderStatus.OPEN, OrderStatus.UNKNOWN, OrderStatus.FILLED, OrderStatus.PENDING}:
                return
        self.orders.submit(
            OrderRequest(
                inst_id=inst,
                side=Side.SELL,
                ord_type=OrderType.MARKET,
                qty=qty,
                client_order_id=cid,
                purpose=order_purpose,
            ),
            now=self._now or opened or datetime.now(UTC),
        )
        self.exchange.fill_at(cid, price)
        self._drain()

    def _ensure_stops(self) -> None:
        for inst, pos in self.book.positions.items():
            if pos.stop_price is None or pos.qty <= 0:
                continue
            cid = make_client_order_id(
                "stop", inst, "sell", int(pos.opened_at.timestamp()), purpose=f"stop-{pos.qty}"
            )
            open_algos = self.exchange.get_open_algos(inst)
            if any(algo.client_order_id == cid and algo.qty == pos.qty for algo in open_algos):
                continue
            if open_algos:
                self.exchange.cancel_algos(inst)
            from okxspot.models import StopRequest

            self.orders.place_stop(
                StopRequest(
                    inst_id=inst,
                    client_order_id=cid,
                    qty=pos.qty,
                    stop_price=pos.stop_price,
                ),
                now=self._now,
            )

    def _drain(self) -> None:
        for fill in self.exchange.pop_new_fills():
            stop, risk = self._entry_meta.get(fill.client_order_id, (None, None))
            closed = self.book.apply_fill(fill, stop_price=stop, initial_risk_quote=risk)
            self.state.save_fill(fill)
            if closed is None:
                continue
            self.trades.append(closed)
            self.risk.register_close(closed.pnl)
            self.exchange.cancel_algos(closed.inst_id)
            self.exchange.cancel_open_orders(closed.inst_id)
            self.alerter.send("info", f"closed {closed.inst_id} pnl={closed.pnl}")

    def _done(self, inst: str, ts: datetime) -> bool:
        return self.state.get_kv(self._processed_key(inst, ts)) == "1"

    def _mark_processed(self, bars: dict[str, Candle]) -> None:
        for inst, bar in bars.items():
            self.state.set_kv(self._processed_key(inst, bar.ts), "1")

    @staticmethod
    def _processed_key(inst: str, ts: datetime) -> str:
        return f"processed:{inst}:{int(ts.timestamp())}"

    def _persist(self) -> None:
        cursor = getattr(self.exchange, "fill_cursor", None)
        self.state.save_book(self.book, fill_cursor=cursor)
        commit = getattr(self.exchange, "commit_fill_cursor", None)
        if callable(commit):
            commit()
        self.state.set_kv("risk_state", self.risk.export_json())
        if isinstance(self.exchange, PaperExchange):
            self.state.save_paper_balances(self.exchange.cash_available, dict(self.exchange.bases))


def build_engine(
    *,
    exchange: Exchange,
    settings: Settings,
    strategy: Strategy,
    state: StateStore,
    fee_rate: Decimal,
    alerter: Alerter | None = None,
    kill_switch: KillSwitch | None = None,
) -> TradingEngine:
    box: list[TradingEngine] = []

    def on_flatten(reason: str) -> None:
        box[0].force_flatten(reason)

    if kill_switch is not None:
        ks = kill_switch
    elif Path(settings.kill_switch_path).exists():
        ks = KillSwitch(settings.kill_switch_path)
    else:
        # Isolated backtests do not create a kill-switch file. Paper/live
        # runners call assert_start_allowed first, which requires a re-armed file.
        ks = None
    journal = OrderJournal(settings.journal_path)
    stale = StaleGuard(
        bar=settings.bar,
        tolerance_sec=settings.stale.tolerance_sec,
        max_clock_skew_ms=settings.stale.max_clock_skew_ms,
        max_price_move_pct=settings.stale.max_price_move_pct,
        max_gap_bars=settings.stale.max_gap_bars,
    )
    risk = RiskManager(settings.risk, on_flatten=on_flatten, kill_switch=ks)
    saved = state.get_kv("risk_state")
    if saved:
        risk.load_json(saved)
    book = state.load_book(settings.allocated_capital)
    engine = TradingEngine(
        exchange=exchange,
        strategy=strategy,
        risk=risk,
        orders=OrderManager(exchange, state, journal=journal, risk=risk),
        book=book,
        alerter=alerter or LogAlerter(),
        settings=settings,
        state=state,
        fee_rate=fee_rate,
        kill_switch=ks,
        stale=stale,
    )
    engine._entry_meta = state.load_meta()
    box.append(engine)
    if engine.risk.halted and engine.book.positions:
        engine.force_flatten("restart_halt")
    return engine


def restore_paper(state: StateStore, paper: PaperExchange) -> None:
    loaded = state.load_paper_balances()
    if loaded is None:
        return
    cash, bases = loaded
    paper.cash_available = cash
    paper.bases = bases
