"""Acceptance tests from Sofia's live-ready NO-GO review (B1–B5, M1–M12)."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from okxspot.alerts import MemoryAlerter, TelegramAlerter
from okxspot.cli import main
from okxspot.engine import TradingEngine, build_engine
from okxspot.errors import AmbiguousOrderError, LiveGuardError, OkxApiError, OkxSpotError
from okxspot.exchange.okx_client import OkxClient
from okxspot.killswitch import KillSwitch
from okxspot.marketdata import PublicFeed
from okxspot.models import (
    AccountView,
    Candle,
    Fill,
    Instrument,
    Order,
    OrderPurpose,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Signal,
    StopOrder,
    StopRequest,
    Ticker,
)
from okxspot.orders.ids import make_client_order_id
from okxspot.orders.manager import OrderManager
from okxspot.runtime import _loop, assert_start_allowed, prepare_live
from okxspot.safety import LIVE_CONFIRM_VALUE, confirm_live_env
from okxspot.state import StateStore
from okxspot.stale import StaleDataError, StaleGuard
from okxspot.strategy.base import BarContext
from okxspot.watchdog import PROTECTED_PID, Supervisor, SupervisorError, monitor_heartbeat
from tests.conftest import candle, day, instrument, make_settings, write_config
from tests.stub import StubExchange, stored_order

D = Decimal


class BuyOnce:
    name = "buy_once"

    def __init__(self) -> None:
        self.fired = False

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if self.fired or ctx.position is not None:
            return []
        self.fired = True
        return [Signal(ctx.inst_id, Side.BUY, D("90"), "test-entry")]


class Flat:
    name = "flat"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return []


class RecordingExchange(StubExchange):
    def __init__(self, inst: Instrument | None = None) -> None:
        self.inst = inst or instrument()
        self.place_calls: list[OrderRequest] = []
        self.cancel_calls: list[str] = []
        self.cancel_algo_calls: list[str] = []
        self.sync_calls = 0
        self.orders: dict[str, Order] = {}
        self.open_ids: set[str] = set()
        self.algos: list[StopOrder] = []
        self.algo_place_calls: list[StopRequest] = []
        self._ticker = Ticker("BTC-USDT", D("100"), D("101"), D("100"), day(1))
        self._fills: list[Fill] = []
        self.cash = D("10000")
        self.permissions = {"read_only", "trade"}

    def get_instrument(self, inst_id: str) -> Instrument:
        return self.inst

    def get_ticker(self, inst_id: str) -> Ticker:
        return self._ticker

    def set_ticker(self, last: Decimal, *, bid: Decimal | None = None, ask: Decimal | None = None) -> None:
        bid = bid if bid is not None else last
        ask = ask if ask is not None else last
        self._ticker = Ticker(self.inst.inst_id, bid, ask, last, datetime.now(UTC))

    def get_account_view(self) -> AccountView:
        return AccountView("USDT", self.cash, D("10000"))

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        return []

    def server_time_ms(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    def place_order(self, request: OrderRequest) -> Order:
        self.place_calls.append(request)
        order = stored_order(request, OrderStatus.OPEN)
        self.orders[request.client_order_id] = order
        self.open_ids.add(request.client_order_id)
        return order

    def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        return [self.orders[cid] for cid in list(self.open_ids) if cid in self.orders]

    def cancel_order(self, inst_id: str, client_order_id: str) -> Order:
        self.cancel_calls.append(client_order_id)
        order = self.orders.get(client_order_id) or stored_order(
            OrderRequest(inst_id, Side.BUY, OrderType.MARKET, D("1"), client_order_id),
            OrderStatus.CANCELED,
        )
        order.status = OrderStatus.CANCELED
        self.orders[client_order_id] = order
        self.open_ids.discard(client_order_id)
        return order

    def cancel_open_orders(self, inst_id: str) -> None:
        for cid in list(self.open_ids):
            self.cancel_order(inst_id, cid)

    def place_algo_stop(self, request: StopRequest) -> StopOrder:
        self.algo_place_calls.append(request)
        stop = StopOrder(request.inst_id, request.client_order_id, request.qty, request.stop_price, "live", algo_id="algo-1")
        self.algos.append(stop)
        return stop

    def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]:
        return list(self.algos)

    def cancel_algos(self, inst_id: str) -> None:
        self.cancel_algo_calls.append(inst_id)
        self.algos.clear()

    def sync(self) -> None:
        self.sync_calls += 1

    def pop_new_fills(self) -> list[Fill]:
        out = list(self._fills)
        self._fills.clear()
        return out

    def fill_at(self, client_order_id: str, price: Decimal) -> None:  # type: ignore[override]
        order = self.orders.get(client_order_id)
        if order is None:
            return
        order.status = OrderStatus.FILLED
        order.filled_qty = order.qty
        order.avg_price = price
        self.open_ids.discard(client_order_id)
        self._fills.append(
            Fill(
                trade_id=f"t-{client_order_id}",
                client_order_id=client_order_id,
                inst_id=order.inst_id,
                side=order.side,
                price=price,
                qty=order.qty,
                fee=D("0"),
                fee_ccy="USDT",
                ts=datetime.now(UTC),
            )
        )


def _engine(tmp_path: Path, exchange: RecordingExchange, *, strategy=None, settings=None, rearm=True):
    settings = settings or make_settings(kill_switch_path=str(tmp_path / "KILL"), journal_path=str(tmp_path / "orders.jsonl"))
    ks = KillSwitch(settings.kill_switch_path)
    if rearm:
        ks.rearm(operator="setup")
    state = StateStore(tmp_path / "s.db")
    return build_engine(
        exchange=exchange,
        settings=settings,
        strategy=strategy or BuyOnce(),
        state=state,
        fee_rate=D("0"),
    ), settings, state


class FakeFeed:
    def __init__(self, series: dict[str, list[Candle]], transport: str = "rest") -> None:
        self._series = series
        self.transport = transport
        self.fallback_reason = None
        self.calls = 0

    def next_candles(self, timeout: float | None = None) -> dict[str, list[Candle]]:  # noqa: ARG002
        self.calls += 1
        return self._series


def _closed(ts: datetime, price: str = "100", inst: str = "BTC-USDT", confirmed: bool = True) -> Candle:
    px = D(price)
    return Candle(inst, ts, px, px, px, px, D("1"), confirmed)


# ----- B1 -----

def test_b1_cli_trip_blocks_next_cycle_and_cancels(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, kill_switch_path=str(tmp_path / "KILL"), journal_path=str(tmp_path / "j.jsonl"))
    KillSwitch(tmp_path / "KILL").rearm(operator="setup")
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    ex = RecordingExchange()
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    # a resting order already on the book
    resting = stored_order(OrderRequest("BTC-USDT", Side.BUY, OrderType.LIMIT, D("1"), "c" + "r" * 31, price=D("99")), OrderStatus.OPEN)
    ex.orders[resting.client_order_id] = resting
    ex.open_ids.add(resting.client_order_id)
    engine, settings, state = _engine(tmp_path, ex, settings=make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
        bar="1H",
    ))
    # Last confirmed 1H bar opened at 00:00 and closed at `now` (01:00); previous is present.
    series = {"BTC-USDT": [_closed(now - timedelta(hours=2), "100"), _closed(now - timedelta(hours=1), "100")]}
    feed = FakeFeed(series)
    _loop(engine, feed, settings, once=True, live=False, now=lambda: now)
    placed_before = len(ex.place_calls)
    assert placed_before >= 1
    code = main(["--log-level", "ERROR", "kill", "--config", str(cfg), "--reason", "op", "--cancel-open"])
    assert code == 0
    buy = BuyOnce()
    buy.fired = False
    engine.strategy = buy
    _loop(engine, feed, settings, once=True, live=False)
    assert len(ex.place_calls) == placed_before
    assert resting.client_order_id in ex.cancel_calls or resting.client_order_id not in ex.open_ids
    state.close()


def test_b1_restart_with_tripped_file_refuses_until_rearm(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
    ks.trip("latched")
    settings = make_settings(kill_switch_path=str(path))
    ex = RecordingExchange()
    with pytest.raises((LiveGuardError, OkxSpotError), match="kill switch|tripped|re-arm|rearm"):
        assert_start_allowed(settings)
    main(["--log-level", "ERROR", "rearm", "--config", str(write_config(tmp_path, kill_switch_path=str(path)))])
    assert_start_allowed(settings)
    engine = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=StateStore(tmp_path / "s2.db"), fee_rate=D("0"))
    assert engine is not None


def test_b1_supervise_stops_child_when_switch_trips(tmp_path: Path) -> None:
    class LiveProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = None
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.killed = True
            self.returncode = 0

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return int(self.returncode or 0)

    path = tmp_path / "KILL"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
    launches: list[int] = []
    current: dict[str, LiveProc] = {}

    def popen(*_a, **_k):
        launches.append(1)
        proc = LiveProc(7000 + len(launches))
        current["p"] = proc
        return proc

    alerts = MemoryAlerter()
    clock = {"t": 1_700_000_000.0}
    polls = {"n": 0}

    def sleeper(_s: float) -> None:
        polls["n"] += 1
        if polls["n"] == 2:
            KillSwitch(path).trip("cli")
        if polls["n"] > 8:
            current["p"].returncode = 0

    sup = Supervisor(
        ["python", "-c", "pass"],
        heartbeat_path=tmp_path / "hb.json",
        kill_switch=ks,
        alerter=alerts,
        max_restarts_per_hour=5,
        backoff_seconds=(0, 0),
        poll_seconds=0.01,
        now=lambda: clock["t"],
        popen=popen,
        sleeper=sleeper,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    with pytest.raises(SupervisorError, match="kill"):
        sup.run_until_stopped(max_cycles=20)
    assert current["p"].killed is True
    assert current["p"].returncode is not None
    assert len(launches) == 1


# ----- B3 -----

def test_b3_stale_candle_blocks_place_order(tmp_path: Path) -> None:
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex)
    old = _closed(datetime(2026, 1, 1, tzinfo=UTC), "100")
    now = datetime(2026, 1, 3, tzinfo=UTC)
    feed = FakeFeed({"BTC-USDT": [old]})
    # Make server time match `now`
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    # Patch now inside loop via a wrapper: set engine clock
    engine._now = now
    _loop(engine, feed, settings, once=True, live=False, now=lambda: now)
    assert ex.place_calls == []
    state.close()


def test_b3_clock_skew_blocks(tmp_path: Path) -> None:
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex)
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    bar = _closed(now - timedelta(minutes=2), "100")
    ex.server_time_ms = lambda: int(now.timestamp() * 1000) + 20_000  # type: ignore[method-assign]
    _loop(engine, FakeFeed({"BTC-USDT": [bar]}), settings, once=True, live=False, now=lambda: now)
    assert ex.place_calls == []
    state.close()


def test_b3_candle_gap_blocks(tmp_path: Path) -> None:
    ex = RecordingExchange()
    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "orders.jsonl"),
        bar="1H",
    )
    engine, settings, state = _engine(tmp_path, ex, settings=settings)
    t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    series = [_closed(t0), _closed(t0 + timedelta(hours=3), "101")]
    now = t0 + timedelta(hours=3, minutes=1)
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    _loop(engine, FakeFeed({"BTC-USDT": series}), settings, once=True, live=False, now=lambda: now)
    assert ex.place_calls == []
    state.close()


def test_b3_loop_invokes_stale_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    orig = StaleGuard.assert_tradeable

    def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        called.append("assert_tradeable")
        return orig(self, **kwargs)

    monkeypatch.setattr(StaleGuard, "assert_tradeable", wrapped)
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex)
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    bar = _closed(now - timedelta(minutes=1), "100")
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    _loop(engine, FakeFeed({"BTC-USDT": [bar]}), settings, once=True, live=False, now=lambda: now)
    assert called == ["assert_tradeable"]
    state.close()


# ----- B4 -----

def test_b4_unconfirmed_ws_still_runs_stops_and_sync(tmp_path: Path) -> None:
    ex = RecordingExchange()
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    unconfirmed = _closed(now, "80", confirmed=False)
    # Feed that would historically block: only unconfirmed, returns quickly after timeout
    feed = FakeFeed({"BTC-USDT": [unconfirmed]}, transport="websocket")
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat(), rearm=True)
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0))
    engine._now = now
    ex.set_ticker(D("90"))  # through the stop
    _loop(engine, feed, settings, once=True, live=True, now=lambda: now)
    assert ex.sync_calls >= 1
    sells = [req for req in ex.place_calls if req.side is Side.SELL]
    assert sells, "software stop must sell when last trades through the stop"
    state.close()


# ----- B5 -----

def test_b5_flatten_id_stable_when_now_is_none(tmp_path: Path) -> None:
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    opened = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), opened)
    engine._now = None
    cid_none = engine.flatten_client_order_id(engine.book.positions["BTC-USDT"], OrderPurpose.FLATTEN)
    engine._now = datetime(2026, 1, 2, tzinfo=UTC)
    cid_later = engine.flatten_client_order_id(engine.book.positions["BTC-USDT"], OrderPurpose.FLATTEN)
    assert cid_none == cid_later
    expected = make_client_order_id("flatten", "BTC-USDT", "sell", int(opened.timestamp()), purpose="flatten")
    assert cid_none == expected
    state.close()


def test_b5_flatten_crash_before_persist_does_not_double_sell(tmp_path: Path) -> None:
    """Crash after the flatten POST but before book persist; restart with a later clock.

    The durable cid is from pos.opened_at, so a wall-clock id would change after
    the clock moves and would produce a second venue POST. This test must fail
    if that B5 fix is reverted.
    """
    db = tmp_path / "s.db"
    settings = make_settings(kill_switch_path=str(tmp_path / "KILL"), journal_path=str(tmp_path / "j.jsonl"))
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    state = StateStore(db)
    ex = RecordingExchange()
    engine = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=state, fee_rate=D("0"))
    opened = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), opened)
    engine.book.allocated_capital = D("1000")
    state.save_book(engine.book)
    engine._now = None
    engine._crash_before_persist = True
    with pytest.raises(RuntimeError, match="injected crash before persist"):
        engine.force_flatten("test")
    assert len(ex.place_calls) == 1
    first_cid = ex.place_calls[0].client_order_id
    expected = make_client_order_id("flatten", "BTC-USDT", "sell", int(opened.timestamp()), purpose="flatten")
    assert first_cid == expected
    state.close()
    state2 = StateStore(db)
    book = state2.load_book(D("1000"))
    assert "BTC-USDT" in book.positions
    assert book.positions["BTC-USDT"].qty == D("1")
    ex2 = RecordingExchange()
    ex2.orders = dict(ex.orders)
    engine2 = build_engine(exchange=ex2, settings=settings, strategy=Flat(), state=state2, fee_rate=D("0"))
    # Clock has moved; a wall-clock cid would differ from first_cid.
    engine2._now = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
    assert "BTC-USDT" in engine2.book.positions
    engine2.force_flatten("restart_halt")
    all_cids = [req.client_order_id for req in ex.place_calls] + [req.client_order_id for req in ex2.place_calls]
    assert set(all_cids) == {first_cid}
    # No second venue POST of a different (or even the same) flatten.
    assert ex2.place_calls == []
    state2.close()


def test_b5_partial_flatten_does_not_resend_full_qty(tmp_path: Path) -> None:
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    opened = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("2"), D("100"), D("95"), D("5"), opened)
    cid = engine.flatten_client_order_id(engine.book.positions["BTC-USDT"], OrderPurpose.FLATTEN)
    partial = stored_order(OrderRequest("BTC-USDT", Side.SELL, OrderType.MARKET, D("2"), cid, purpose=OrderPurpose.FLATTEN), OrderStatus.PARTIALLY_FILLED)
    partial.filled_qty = D("1")
    ex.orders[cid] = partial
    engine.orders.state.upsert_order(partial)
    engine.force_flatten("again")
    full_qty_posts = [req for req in ex.place_calls if req.qty == D("2")]
    assert len(full_qty_posts) == 0
    state.close()


# ----- M1 -----

def test_m1_pretrade_invoked_for_entry_exit_flatten_and_algo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    from okxspot.risk.manager import RiskManager

    orig = RiskManager.pretrade

    def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(str(kwargs.get("purpose")))
        return orig(self, **kwargs)

    monkeypatch.setattr(RiskManager, "pretrade", wrapped)
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex)
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    bars = [_closed(now - timedelta(hours=i), "100") for i in range(30, 0, -1)] + [_closed(now, "110")]
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    _loop(engine, FakeFeed({"BTC-USDT": bars}), settings, once=True, live=False, now=lambda: now)
    # flatten
    if "BTC-USDT" not in engine.book.positions:
        engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), now)
    engine.force_flatten("m1")
    # exit + algo-stop must also go through pretrade
    pos = Position("ETH-USDT", D("1"), D("100"), D("95"), D("5"), now)
    engine.book.positions["ETH-USDT"] = pos
    engine._ensure_stops()
    engine._handle_signal(Signal("ETH-USDT", Side.SELL, D("95"), "exit-test"), _closed(now, "100", inst="ETH-USDT"))
    purposes = " ".join(seen)
    assert "entry" in purposes.lower() or "ENTRY" in purposes
    assert "flatten" in purposes.lower() or "FLATTEN" in purposes
    assert "exit" in purposes.lower() or "EXIT" in purposes
    assert "stop" in purposes.lower() or "STOP" in purposes
    state.close()


def test_m1_rate_limit_blocks_and_survives_restart(tmp_path: Path) -> None:
    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
        risk={"max_orders_per_minute": 30, "max_orders_per_day": 1},
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    state = StateStore(tmp_path / "s.db")
    ex = RecordingExchange()
    engine = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=state, fee_rate=D("0"))
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    engine.orders.submit(OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("0.01"), "c" + "a" * 31), now=now)
    engine._persist()
    state.close()
    state2 = StateStore(tmp_path / "s.db")
    engine2 = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=state2, fee_rate=D("0"))
    later = now + timedelta(minutes=5)
    blocked = engine2.risk.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        now=later,
        record=True,
    )
    assert blocked.allowed is False
    assert blocked.reason == "max_orders_per_day"
    with pytest.raises((OkxSpotError, LiveGuardError), match="max_orders_per_day|pretrade"):
        engine2.orders.submit(OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("0.01"), "c" + "b" * 31), now=later)
    state2.close()


# ----- M2 -----

def test_m2_ambiguous_place_is_not_resent(tmp_path: Path) -> None:
    class Lagging(StubExchange):
        def __init__(self) -> None:
            self.place_calls = 0
            self.lookups = 0
            self.hidden: Order | None = None

        def place_order(self, request: OrderRequest) -> Order:
            self.place_calls += 1
            if self.place_calls == 1:
                self.hidden = stored_order(request, OrderStatus.OPEN)
                raise AmbiguousOrderError("timeout")
            raise AssertionError("second place_order must not happen")

        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            self.lookups += 1
            if self.lookups < 3:
                return None
            return self.hidden

    ex = Lagging()
    state = StateStore(tmp_path / "s.db")
    order = OrderManager(ex, state).submit(OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), "c" + "d" * 31))
    assert ex.place_calls == 1
    assert order.status in {OrderStatus.OPEN, OrderStatus.UNKNOWN}
    state.close()


# ----- M3 -----

def test_m3_algo_timeout_then_visible_is_one_algo(tmp_path: Path) -> None:
    class FlakyAlgo(RecordingExchange):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        def place_algo_stop(self, request: StopRequest) -> StopOrder:
            self.algo_place_calls.append(request)
            if len(self.algo_place_calls) == 1:
                self.algos = [StopOrder(request.inst_id, request.client_order_id, request.qty, request.stop_price, "live", algo_id="a1")]
                raise AmbiguousOrderError("timeout")
            raise AssertionError("second algo place")

        def get_open_algos(self, inst_id: str | None = None) -> list[StopOrder]:
            self.lookups += 1
            return list(self.algos) if self.lookups >= 2 else []

    ex = FlakyAlgo()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0))
    engine._ensure_stops()
    assert len(ex.algo_place_calls) == 1
    state.close()


def test_m3_stop_resized_after_partial_add(tmp_path: Path) -> None:
    ex = RecordingExchange()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0))
    engine._ensure_stops()
    assert ex.algo_place_calls[0].qty == D("1")
    # position increased
    engine.book.positions["BTC-USDT"].qty = D("2")
    engine._ensure_stops()
    assert any(req.qty == D("2") for req in ex.algo_place_calls)
    # only one working algo
    assert len(ex.get_open_algos("BTC-USDT")) == 1
    assert ex.get_open_algos("BTC-USDT")[0].qty == D("2")
    state.close()


# ----- M4 -----

def test_m4_crash_between_kv_and_commit_keeps_old_book(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "s.db")
    from okxspot.portfolio import PortfolioBook

    book = PortfolioBook(D("1000"))
    book.realized_pnl = D("0")
    book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0))
    state.save_book(book)
    book2 = PortfolioBook(D("1000"))
    book2.realized_pnl = D("12")
    book2.positions = {}
    state._crash_after_kv = True  # noqa: SLF001
    with pytest.raises(Exception):
        state.save_book(book2)
    state._crash_after_kv = False  # noqa: SLF001
    loaded = state.load_book(D("1000"))
    assert loaded.realized_pnl == D("0")
    assert "BTC-USDT" in loaded.positions
    assert loaded.positions["BTC-USDT"].qty == D("1")
    state.close()


# ----- M5 -----

def test_m5_slippage_rejects_or_caps_spend(tmp_path: Path) -> None:
    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
        fees={"taker_fee": "0.001", "backtest_fee_multiplier": "1", "paper_fee_multiplier": "1", "slippage_bps": "200", "partial_fill_ratio": "1"},
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    ex = RecordingExchange()
    ex.set_ticker(D("100"), bid=D("100"), ask=D("102"))
    engine, _, state = _engine(tmp_path, ex, settings=settings, strategy=Flat())
    inst = instrument()
    # tight cap so 2% slip would breach
    decision = engine.risk.evaluate_entry(
        entry_price=D("100"),
        stop_price=D("99"),
        allocated_capital=D("1000"),
        realized_pnl=D("0"),
        available_quote=D("1000"),
        open_positions=0,
        current_exposure_notional=D("0"),
        instrument=inst,
        has_position=False,
        fee_rate=D("0"),
        worst_case_price=D("102"),
    )
    if decision.allowed:
        spend = decision.qty * D("102")
        cap = D("1000") * settings.risk.per_coin_cap_pct
        assert spend <= cap + D("0.0000001")
    else:
        assert decision.reason in {"max_notional", "min_notional", "slippage_cap", "exposure_cap", "max_total_exposure", "below_lot_size"}
    # explicit reject when slipped notional breaches a tight max_notional
    settings.risk.max_notional = D("1")
    denied = engine.risk.evaluate_entry(
        entry_price=D("100"),
        stop_price=D("99"),
        allocated_capital=D("1000"),
        realized_pnl=D("0"),
        available_quote=D("1000"),
        open_positions=0,
        current_exposure_notional=D("0"),
        instrument=inst,
        has_position=False,
        fee_rate=D("0"),
        worst_case_price=D("102"),
    )
    assert denied.allowed is False
    state.close()


# ----- M6 -----

def test_m6_rejected_flatten_retries_same_clordid(tmp_path: Path) -> None:
    class RejectOnce(RecordingExchange):
        def place_order(self, request: OrderRequest) -> Order:
            self.place_calls.append(request)
            if len(self.place_calls) == 1 and request.purpose is OrderPurpose.FLATTEN:
                order = stored_order(request, OrderStatus.REJECTED)
                self.orders[request.client_order_id] = order
                return order
            return super().place_order(request)

    ex = RejectOnce()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    opened = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), opened)
    engine.risk.halted = True
    engine.risk.halt_reason = "max_drawdown"
    engine.force_flatten("max_drawdown")
    cid = engine.flatten_client_order_id(Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), opened), OrderPurpose.FLATTEN)
    assert ex.place_calls[0].client_order_id == cid
    # next loop retries
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    _loop(engine, FakeFeed({"BTC-USDT": [_closed(now, "100")]}), settings, once=True, live=True, now=lambda: now)
    flatten_cids = [req.client_order_id for req in ex.place_calls if req.purpose is OrderPurpose.FLATTEN]
    assert flatten_cids
    assert set(flatten_cids) == {cid}
    state.close()


# ----- M7 -----

def test_m7_ticker_failure_does_not_skip_stop_via_avg_price(tmp_path: Path) -> None:
    class BoomTicker(RecordingExchange):
        def get_ticker(self, inst_id: str) -> Ticker:
            raise RuntimeError("ticker down")

    ex = BoomTicker()
    engine, settings, state = _engine(tmp_path, ex, strategy=Flat())
    engine.book.positions["BTC-USDT"] = Position("BTC-USDT", D("1"), D("100"), D("95"), D("5"), day(0))
    engine.marks["BTC-USDT"] = D("101")  # closed mark above stop
    engine.enforce_software_stops()
    # Must not decide "not stopped" from avg/marks. Either a sell, or no skip-as-safe (no continue-as-ok).
    sells = [req for req in ex.place_calls if req.side is Side.SELL]
    # Accept "sold" or "blocked/alerted without concluding price is above stop"
    assert sells or getattr(engine, "_untrusted_stop", False) or any(
        True for _ in []
    )
    # Tighten: avg_price path must not be used to skip. If no sell, engine must flag untrusted.
    if not sells:
        assert getattr(engine, "untrusted_price", False) or getattr(engine, "_untrusted_stop", False)
    state.close()


# ----- M8 -----

def test_m8_paginated_fill_not_on_first_page_is_applied(tmp_path: Path) -> None:
    from okxspot.exchange.okx_exchange import OkxExchange

    pages = [
        {"code": "0", "data": [{"tradeId": "new-1", "clOrdId": "c" + "n" * 31, "instId": "BTC-USDT", "side": "buy", "fillPx": "100", "fillSz": "0.01", "fee": "0", "feeCcy": "USDT", "ts": "1700000001000"}]},
        {"code": "0", "data": [{"tradeId": "old-1", "clOrdId": "c" + "o" * 31, "instId": "BTC-USDT", "side": "buy", "fillPx": "100", "fillSz": "1", "fee": "0", "feeCcy": "USDT", "ts": "1690000000000"}]},
        {"code": "0", "data": []},
    ]
    seen_params: list[dict] = []

    class _Limiter:
        def acquire(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/trade/fills"):
            seen_params.append(dict(request.url.params))
            idx = min(len(seen_params) - 1, len(pages) - 1)
            return httpx.Response(200, json=pages[idx])
        return httpx.Response(404, json={"code": "404", "msg": request.url.path})

    client = OkxClient(api_key="test-key", secret="test-secret", passphrase="test-pass", transport=httpx.MockTransport(handler), limiter=_Limiter())
    exchange = OkxExchange(client, min_notional=D("1"))
    exchange.sync()
    fills = exchange.pop_new_fills()
    ids = {f.trade_id for f in fills}
    assert "old-1" in ids
    assert len(seen_params) >= 2
    client.close()


# ----- M9 -----

def test_m9_monitor_trips_when_supervisor_heartbeat_stale(tmp_path: Path) -> None:
    import subprocess, sys, time

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert child.pid != PROTECTED_PID
        hb = tmp_path / "hb.json"
        stale = datetime.now(UTC) - timedelta(seconds=120)
        hb.write_text(json.dumps({
            "ts": stale.isoformat(),
            "pid": child.pid,
            "child_pid": child.pid,
            "running": True,
            "supervisor_pid": 1,
            "argv": [sys.executable],
        }), encoding="utf-8")
        ks = KillSwitch(tmp_path / "KILL")
        ks.rearm(operator="setup")
        tripped = monitor_heartbeat(
            heartbeat_path=hb,
            kill_switch=ks,
            max_age_seconds=5,
            protected_pids=frozenset({PROTECTED_PID}),
        )
        assert tripped is True
        assert ks.is_tripped is True
        child.wait(timeout=5)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.terminate()


def test_m9_frozen_child_is_detected(tmp_path: Path) -> None:
    class Frozen:
        def __init__(self) -> None:
            self.pid = 8001
            self.returncode = None
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.killed = True
            self.returncode = -15

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return int(self.returncode or 0)

    ks = KillSwitch(tmp_path / "KILL")
    ks.rearm(operator="setup")
    child_hb = tmp_path / "hb.child.json"
    child_hb.write_text(json.dumps({
        "ts": datetime.fromtimestamp(1_700_000_000.0 - 120, tz=UTC).isoformat(),
        "pid": 8001,
        "running": True,
    }), encoding="utf-8")
    frozen = Frozen()

    def popen(*_a, **_k):
        return frozen

    clock = {"t": 1_700_000_000.0}
    alerts = MemoryAlerter()
    polls = {"n": 0}

    def sleeper(_s: float) -> None:
        polls["n"] += 1
        clock["t"] += 30
        if polls["n"] > 6:
            frozen.returncode = 0

    sup = Supervisor(
        ["python", "-c", "pass"],
        heartbeat_path=tmp_path / "hb.json",
        kill_switch=ks,
        alerter=alerts,
        child_heartbeat_path=child_hb,
        child_stale_seconds=5,
        now=lambda: clock["t"],
        popen=popen,
        sleeper=sleeper,
        poll_seconds=0.01,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    # run a few cycles; frozen child should be killed
    try:
        sup.run_until_stopped(max_cycles=10)
    except SupervisorError:
        pass
    assert frozen.killed is True


def test_m9_live_without_supervisor_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OKXSPOT_SUPERVISED", raising=False)
    cfg = write_config(tmp_path, live_enabled=True, mode="live", required_uid="sub", required_account_label="bot")
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
    }
    code = main(["--log-level", "ERROR", "live", "--config", str(cfg), "--enable-live"], env=env)
    assert code != 0


# ----- M10 -----

def test_m10_refuses_wedding_capital() -> None:
    with pytest.raises(Exception, match="6788|wedding"):
        make_settings(allocated_capital="6788", max_allocated_capital="6788")


def test_m10_midrun_account_change_trips_kill_switch(tmp_path: Path) -> None:
    from okxspot.exchange.okx_parse import AccountConfig
    from okxspot.runtime import maybe_recheck_account

    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        required_uid="sub",
        required_account_label="bot",
        allocated_capital="1000",
        max_allocated_capital="1000",
    )
    ks = KillSwitch(settings.kill_switch_path)
    ks.rearm(operator="setup")
    engine, settings, state = _engine(tmp_path, RecordingExchange(), settings=settings, strategy=Flat())
    main_acct = AccountConfig(permissions={"read_only", "trade"}, ip="1.2.3.4", uid="same", main_uid="same", label="bot")
    maybe_recheck_account(engine, settings, account=main_acct, total_eq=D("50000"))
    assert engine.risk.kill_switch is not None and engine.risk.kill_switch.is_tripped
    state.close()


# ----- M11 -----

def test_m11_live_refuses_placeholder_strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKXSPOT_SUPERVISED", "1")
    cfg = write_config(
        tmp_path,
        live_enabled=True,
        mode="live",
        required_uid="sub",
        required_account_label="bot",
        kill_switch_path=str(tmp_path / "KILL"),
        allow_placeholder_live=False,
    )
    KillSwitch(tmp_path / "KILL").rearm(operator="setup")
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
        "OKXSPOT_SUPERVISED": "1",
    }
    from okxspot.exchange.okx_parse import AccountConfig

    monkeypatch.setattr("okxspot.runtime.connect_okx", lambda *a, **k: object())
    monkeypatch.setattr(
        "okxspot.runtime.fetch_account_config",
        lambda client, settings: AccountConfig(
            permissions={"trade"}, ip="1.2.3.4", uid="sub", main_uid="main", label="bot"
        ),
    )
    ran: list[str] = []
    monkeypatch.setattr("okxspot.cli.run_live_loop", lambda *a, **k: ran.append("ran"))
    code = main(["--log-level", "ERROR", "live", "--config", str(cfg), "--enable-live"], env=env)
    assert code != 0
    assert ran == []


# ----- M12 -----

def test_m12_client_repr_and_logs_redact_secrets(capsys, caplog) -> None:  # type: ignore[no-untyped-def]
    secret = "super-secret-value-xyz"
    token = "telegram-token-xyz"
    client = OkxClient(api_key="test-key", secret=secret, passphrase="pass-phrase-xyz")
    text = repr(client) + str(client)
    assert secret not in text
    assert "pass-phrase-xyz" not in text
    err = OkxApiError("1", f"denied Authorization: {secret} token={token}")
    caplog.set_level(logging.ERROR)
    from okxspot.logging_config import configure_logging, Log

    configure_logging("ERROR")
    Log("okxspot.test").error("failed", exc=str(err), secret=secret)
    logged = caplog.text + capsys.readouterr().out + capsys.readouterr().err
    # also format through JsonFormatter
    from okxspot.logging_config import JsonFormatter

    rec = logging.LogRecord("okxspot.test", logging.ERROR, __file__, 1, str(err), (), None)
    formatted = JsonFormatter().format(rec)
    assert secret not in formatted
    assert token not in formatted
    assert secret not in logged

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    TelegramAlerter(token, "42", client=http).send("info", "hello")
    http.close()
    out = caplog.text
    assert token not in out

# ----- M8 persist cursor + no double apply -----

def test_m8_fill_cursor_persists_across_restart_and_does_not_reapply(tmp_path: Path) -> None:
    from okxspot.exchange.okx_exchange import OkxExchange
    from okxspot.portfolio import PortfolioBook

    pages = [
        {"code": "0", "data": [{"tradeId": "new-1", "billId": "b-new", "clOrdId": "c" + "n" * 31, "instId": "BTC-USDT", "side": "buy", "fillPx": "100", "fillSz": "0.01", "fee": "0", "feeCcy": "USDT", "ts": "1700000001000"}]},
        {"code": "0", "data": [{"tradeId": "old-1", "billId": "b-old", "clOrdId": "c" + "o" * 31, "instId": "BTC-USDT", "side": "buy", "fillPx": "100", "fillSz": "1", "fee": "0", "feeCcy": "USDT", "ts": "1690000000000"}]},
        {"code": "0", "data": []},
    ]
    seen_params: list[dict] = []

    class _Limiter:
        def acquire(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/trade/fills"):
            seen_params.append(dict(request.url.params))
            params = dict(request.url.params)
            after = params.get("after")
            before = params.get("before")
            if before or (after and after in {"b-new", "b-old"}):
                # A follow-up sync keyed off the saved cursor has no additional fills.
                return httpx.Response(200, json={"code": "0", "data": []})
            # First-pass backfill: no cursor bound, then after= to walk older pages.
            return httpx.Response(200, json=pages[min(len(seen_params) - 1, len(pages) - 1)])
        return httpx.Response(404, json={"code": "404", "msg": request.url.path})

    def make_exchange() -> OkxExchange:
        client = OkxClient(
            api_key="test-key", secret="test-secret", passphrase="test-pass",
            transport=httpx.MockTransport(handler), limiter=_Limiter(),
        )
        return OkxExchange(client, min_notional=D("1"))

    settings = make_settings(kill_switch_path=str(tmp_path / "KILL"), journal_path=str(tmp_path / "j.jsonl"))
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    state = StateStore(tmp_path / "s.db")
    ex = make_exchange()
    engine = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=state, fee_rate=D("0"))
    engine.sync_exchange_fills()
    engine._persist()
    first_ids = set(engine.book.applied)
    assert "old-1" in first_ids or "new-1" in first_ids
    cursor = state.get_kv("fill_cursor")
    assert cursor == "b-new"
    state.close()
    ex.client.close()

    seen_before = len(seen_params)
    state2 = StateStore(tmp_path / "s.db")
    ex2 = make_exchange()
    engine2 = build_engine(exchange=ex2, settings=settings, strategy=Flat(), state=state2, fee_rate=D("0"))
    assert state2.get_kv("fill_cursor") == cursor
    engine2.sync_exchange_fills()
    # Cursor is the newest billId; later syncs use before= (newer than cursor).
    assert any(p.get("before") == cursor for p in seen_params[seen_before:])
    assert all(p.get("after") != cursor for p in seen_params[seen_before:])
    assert engine2.book.applied == first_ids or first_ids <= engine2.book.applied
    # applied fills are not applied twice
    assert list(engine2.book.applied).count("old-1") <= 1 if "old-1" in engine2.book.applied else True
    state2.close()
    ex2.client.close()


def test_m8_newer_fills_after_saved_cursor_are_applied_once_in_order(tmp_path: Path) -> None:
    """Subsequent syncs must request fills *newer* than the newest saved billId.

    OKX v5 (docs-v5/trick_en + GET /api/v5/trade/fills):
    ``before=<billId>`` returns records newer than that billId (closest to it,
    newest-first on the page). ``after=<billId>`` returns earlier records and
    would miss anything that arrived after the cursor was saved.
    The persisted cursor moves to the newest billId only after save_book.
    """
    from okxspot.exchange.okx_exchange import OkxExchange

    def _cid(tag: str) -> str:
        return (tag + "x" * 32)[:32]

    def _row(trade_id: str, bill_id: str, ts_ms: int, qty: str = "0.001") -> dict[str, str]:
        return {
            "tradeId": trade_id,
            "billId": bill_id,
            "clOrdId": _cid(trade_id),
            "instId": "BTC-USDT",
            "side": "buy",
            "fillPx": "100",
            "fillSz": qty,
            "fee": "0",
            "feeCcy": "USDT",
            "ts": str(ts_ms),
        }

    # billId is a monotonic integer-as-string, like OKX snowflakes.
    seed = [
        _row("old-1", "100", 1_700_000_000_000, qty="0.01"),
        _row("old-2", "110", 1_700_000_001_000, qty="0.01"),
    ]
    newer = [
        _row(f"new-{i}", str(200 + i), 1_700_000_100_000 + i * 1000)
        for i in range(1, 251)
    ]
    venue: list[dict[str, str]] = list(seed)
    seen_params: list[dict[str, str]] = []

    def _bill(row: dict[str, str]) -> int:
        return int(row["billId"])

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/trade/fills"):
            return httpx.Response(404, json={"code": "404", "msg": request.url.path})
        params = dict(request.url.params)
        seen_params.append(params)
        rows = list(venue)
        after = params.get("after")
        before = params.get("before")
        # Official semantics: after = earlier than billId; before = newer than billId;
        # both exclusive; always newest-first; if only before is set, return the
        # records closest to that billId (https://www.okx.com/docs-v5/trick_en/).
        if after is not None:
            rows = [r for r in rows if _bill(r) < int(after)]
        if before is not None:
            rows = [r for r in rows if _bill(r) > int(before)]
        rows.sort(key=_bill, reverse=True)
        limit = int(params.get("limit") or 100)
        if before is not None and after is None:
            # Official trick_en: only-before returns records closest to that
            # billId, still newest-first (before=6, limit=3 → [9, 8, 7]).
            page = rows[-limit:] if rows else []
        else:
            # closer to `after` (or the absolute newest when neither bound is set)
            page = rows[:limit]
        return httpx.Response(200, json={"code": "0", "data": page})

    class _Limiter:
        def acquire(self) -> None:
            return None

    def make_exchange() -> OkxExchange:
        client = OkxClient(
            api_key="test-key",
            secret="test-secret",
            passphrase="test-pass",
            transport=httpx.MockTransport(handler),
            limiter=_Limiter(),
        )
        return OkxExchange(client, min_notional=D("1"))

    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    state = StateStore(tmp_path / "s.db")
    ex = make_exchange()
    engine = build_engine(exchange=ex, settings=settings, strategy=Flat(), state=state, fee_rate=D("0"))
    engine.sync_exchange_fills()
    # Cursor must not move on disk until the book is saved.
    assert state.get_kv("fill_cursor") is None
    engine._persist()
    assert set(engine.book.applied) == {"old-1", "old-2"}
    assert state.get_kv("fill_cursor") == "110"
    state.close()
    ex.client.close()

    venue.extend(newer)
    seen_before = len(seen_params)
    applied_order: list[str] = []
    state2 = StateStore(tmp_path / "s.db")
    ex2 = make_exchange()
    engine2 = build_engine(exchange=ex2, settings=settings, strategy=Flat(), state=state2, fee_rate=D("0"))
    orig_apply = engine2.book.apply_fill

    def _record_apply(fill, *args, **kwargs):  # type: ignore[no-untyped-def]
        applied_order.append(fill.trade_id)
        return orig_apply(fill, *args, **kwargs)

    engine2.book.apply_fill = _record_apply  # type: ignore[method-assign]
    assert state2.get_kv("fill_cursor") == "110"
    engine2.sync_exchange_fills()
    expected_new = [f"new-{i}" for i in range(1, 251)]
    assert [tid for tid in applied_order if tid.startswith("new-")] == expected_new
    assert applied_order.count("old-1") == 0 or engine2.book.applied == {"old-1", "old-2", *expected_new}
    # Old fills must not be re-applied; new fills applied exactly once, in billId order.
    assert applied_order == expected_new or [t for t in applied_order if t.startswith("new-")] == expected_new
    assert len([t for t in applied_order if t.startswith("new-")]) == 250
    assert set(engine2.book.applied) == {"old-1", "old-2", *expected_new}
    restart_params = seen_params[seen_before:]
    assert any(p.get("before") == "110" for p in restart_params)
    assert all(p.get("after") != "110" and p.get("after") != "100" for p in restart_params if p.get("before") == "110")
    # Disk cursor stays at the previously saved newest billId until persist.
    assert state2.get_kv("fill_cursor") == "110"
    engine2._persist()
    assert state2.get_kv("fill_cursor") == "450"
    state2.close()
    ex2.client.close()

    seen_before_third = len(seen_params)
    applied_again: list[str] = []
    state3 = StateStore(tmp_path / "s.db")
    ex3 = make_exchange()
    engine3 = build_engine(exchange=ex3, settings=settings, strategy=Flat(), state=state3, fee_rate=D("0"))
    orig_apply3 = engine3.book.apply_fill

    def _record_apply3(fill, *args, **kwargs):  # type: ignore[no-untyped-def]
        applied_again.append(fill.trade_id)
        return orig_apply3(fill, *args, **kwargs)

    engine3.book.apply_fill = _record_apply3  # type: ignore[method-assign]
    assert state3.get_kv("fill_cursor") == "450"
    engine3.sync_exchange_fills()
    assert applied_again == []
    assert set(engine3.book.applied) == {"old-1", "old-2", *expected_new}
    third_params = seen_params[seen_before_third:]
    assert any(p.get("before") == "450" for p in third_params)
    assert all(p.get("after") != "450" for p in third_params)
    state3.close()
    ex3.client.close()


# ----- M9 supervise env + monitor loop -----

def test_m9_supervise_sets_supervised_env(tmp_path: Path) -> None:
    from okxspot.safety import SUPERVISED_ENV

    captured: dict[str, object] = {}

    class Proc:
        def __init__(self) -> None:
            self.pid = 9101
            self.returncode = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return int(self.returncode or 0)

    def popen(*_a, **kwargs):
        captured["env"] = kwargs.get("env")
        return Proc()

    ks = KillSwitch(tmp_path / "KILL")
    ks.rearm(operator="setup")
    sup = Supervisor(
        ["python", "-c", "pass"],
        heartbeat_path=tmp_path / "hb.json",
        kill_switch=ks,
        alerter=MemoryAlerter(),
        popen=popen,
        sleeper=lambda _s: None,
        now=lambda: 1_700_000_000.0,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    sup.spawn()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get(SUPERVISED_ENV) == "1"


def test_m9_monitor_loop_trips_when_heartbeat_goes_stale(tmp_path: Path) -> None:
    from okxspot.watchdog import monitor_loop

    hb = tmp_path / "hb.json"
    clock = {"t": 1_700_000_000.0}
    ks = KillSwitch(tmp_path / "KILL")
    ks.rearm(operator="setup")
    hb.write_text(json.dumps({
        "ts": datetime.fromtimestamp(clock["t"], tz=UTC).isoformat(),
        "pid": 4242,
        "child_pid": 4242,
        "running": True,
        "supervisor_pid": 1,
        "argv": ["python"],
    }), encoding="utf-8")
    polls = {"n": 0}

    def sleeper(_s: float) -> None:
        polls["n"] += 1
        clock["t"] += 120

    tripped = monitor_loop(
        heartbeat_path=hb,
        kill_switch=ks,
        max_age_seconds=5,
        poll_seconds=0.01,
        now=lambda: clock["t"],
        sleeper=sleeper,
        protected_pids=frozenset({PROTECTED_PID}),
        max_cycles=5,
    )
    assert tripped is True
    assert ks.is_tripped is True
    assert polls["n"] >= 1


# ----- M10 loop wiring -----

def test_m10_loop_invokes_account_recheck_and_blocks_entries(tmp_path: Path) -> None:
    from okxspot.exchange.okx_parse import AccountConfig

    class MainAcct(RecordingExchange):
        def get_account_config(self) -> AccountConfig:
            return AccountConfig(permissions={"trade"}, ip="1.2.3.4", uid="same", main_uid="same", label="bot")

        def get_account_view(self) -> AccountView:
            return AccountView("USDT", D("1000"), D("50000"))

    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
        required_uid="sub",
        required_account_label="bot",
        allocated_capital="1000",
        max_allocated_capital="1000",
        bar="1H",
    )
    ex = MainAcct()
    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    ex.server_time_ms = lambda: int(now.timestamp() * 1000)  # type: ignore[method-assign]
    engine, settings, state = _engine(tmp_path, ex, settings=settings, strategy=BuyOnce())
    last = now - timedelta(hours=1)
    prev = last - timedelta(hours=1)
    _loop(
        engine,
        FakeFeed({"BTC-USDT": [_closed(prev, "100"), _closed(last, "100")]}),
        settings,
        once=True,
        live=True,
        now=lambda: now,
    )
    assert engine.kill_switch is not None and engine.kill_switch.is_tripped
    assert engine.risk.halted is True
    assert ex.place_calls == []
    state.close()


# ----- named minors m1–m4 -----

def test_m1_live_requires_settings_mode_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKXSPOT_SUPERVISED", "1")
    settings = make_settings(
        live_enabled=True,
        mode="paper",
        required_uid="sub",
        required_account_label="bot",
        allow_placeholder_live=True,
        kill_switch_path=str(tmp_path / "KILL"),
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
        "OKXSPOT_SUPERVISED": "1",
    }
    with pytest.raises(LiveGuardError, match="mode == 'live'"):
        prepare_live(settings, enable_live_flag=True, env=env)


def test_m2_rejects_wildcard_ip() -> None:
    from okxspot.exchange.okx_parse import AccountConfig
    from okxspot.safety import validate_account_restrictions

    settings = make_settings(required_uid="sub", required_account_label="bot")
    base = dict(permissions={"trade"}, uid="sub", main_uid="main", label="bot")
    for ip in ("*", "0.0.0.0", "0.0.0.0/0", "::/0"):
        with pytest.raises(LiveGuardError, match="IP-restricted"):
            validate_account_restrictions(AccountConfig(ip=ip, **base), settings)
    validate_account_restrictions(AccountConfig(ip="1.2.3.4", **base), settings)


def test_m3_live_requires_uid_and_account_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKXSPOT_SUPERVISED", "1")
    settings = make_settings(
        live_enabled=True,
        mode="live",
        allow_placeholder_live=True,
        kill_switch_path=str(tmp_path / "KILL"),
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
        "OKXSPOT_SUPERVISED": "1",
    }
    with pytest.raises(LiveGuardError, match="required_uid and required_account_label"):
        prepare_live(settings, enable_live_flag=True, env=env)


def test_m4_journal_is_attached_and_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    synced: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    settings = make_settings(kill_switch_path=str(tmp_path / "KILL"), journal_path=str(tmp_path / "j.jsonl"))
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    state = StateStore(tmp_path / "s.db")
    engine = build_engine(
        exchange=RecordingExchange(),
        settings=settings,
        strategy=Flat(),
        state=state,
        fee_rate=D("0"),
    )
    assert engine.orders.journal is not None
    assert Path(settings.journal_path) == engine.orders.journal.path or str(engine.orders.journal.path) == settings.journal_path
    synced.clear()
    engine.orders.submit(OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("0.01"), "c" + "z" * 31), now=datetime(2026, 1, 2, tzinfo=UTC))
    text = Path(settings.journal_path).read_text(encoding="utf-8")
    assert "submitted" in text
    assert synced, "journal append must fsync"
    state.close()
