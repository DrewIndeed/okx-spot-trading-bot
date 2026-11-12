"""Live NO-GO guards from Sofia's e95875d addendum.

Guard 1: /trade/fills only covers 3 days. A stale or missing last-sync time must
backfill from /trade/fills-history or refuse, never silently resume with a gap.
Guard 2: exchange balances for traded assets must match the bot ledger within a
per-asset tolerance; a mismatch trips the kill switch and needs a manual reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest

from okxspot.alerts import MemoryAlerter
from okxspot.cli import main
from okxspot.engine import TradingEngine, build_engine
from okxspot.errors import LiveGuardError
from okxspot.exchange.okx_client import OkxClient
from okxspot.exchange.okx_exchange import OkxExchange
from okxspot.killswitch import KillSwitch
from okxspot.live_guards import (
    FILL_SYNC_TS_KEY,
    FillGapError,
    fill_sync_decision,
    position_mismatches,
)
from okxspot.models import Fill, Order, OrderRequest, Side
from okxspot.portfolio import PortfolioBook
from okxspot.runtime import assert_start_allowed
from okxspot.state import StateStore
from okxspot.strategy.base import BarContext, Signal
from tests.conftest import make_settings, write_config
from tests.stub import StubExchange, stored_order

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


class Flat:
    name = "flat"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return []


def _settings(tmp_path: Path, **guards: object) -> Any:
    settings = make_settings(
        kill_switch_path=str(tmp_path / "KILL"),
        journal_path=str(tmp_path / "j.jsonl"),
        guards=guards,
    )
    KillSwitch(settings.kill_switch_path).rearm(operator="setup")
    return settings


class FakeLive(StubExchange):
    """Live-shaped exchange: has a fill cursor, optional history, and balances."""

    def __init__(self, *, history: bool = True, balances: dict[str, D] | None = None) -> None:
        self.cursor: str | None = None
        self.calls: list[str] = []
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.balances = balances or {}
        self._fills: list[Fill] = []
        if not history:
            self.sync_history = None  # type: ignore[assignment]

    def load_fill_cursor(self, cursor: str | None) -> None:
        self.cursor = cursor

    @property
    def fill_cursor(self) -> str | None:
        return self.cursor

    def commit_fill_cursor(self) -> None:
        return None

    def sync(self) -> None:
        self.calls.append("fills")

    def sync_history(self) -> None:  # type: ignore[no-redef]
        self.calls.append("fills-history")

    def pop_new_fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out

    def get_balances(self, ccys: list[str]) -> dict[str, D]:
        self.calls.append("balance")
        return {c: self.balances.get(c, D("0")) for c in ccys}

    def place_order(self, request: OrderRequest) -> Order:
        self.placed.append(request)
        return stored_order(request)

    def cancel_open_orders(self, inst_id: str) -> None:
        self.cancelled.append(inst_id)


def _engine(tmp_path: Path, ex: Any, settings: Any, alerter: MemoryAlerter | None = None) -> TradingEngine:
    state = StateStore(tmp_path / "s.db")
    return build_engine(
        exchange=ex,
        settings=settings,
        strategy=Flat(),
        state=state,
        fee_rate=D("0"),
        alerter=alerter or MemoryAlerter(),
    )


def _buy(qty: str = "0.01", fee: str = "0", fee_ccy: str = "USDT", tid: str = "t1") -> Fill:
    return Fill(tid, "cid", "BTC-USDT", Side.BUY, D("50000"), D(qty), D(fee), fee_ccy, NOW - timedelta(hours=1))


# ---------- Guard 1: decision table ----------


def test_fill_window_recent_sync_uses_fills() -> None:
    settings = make_settings().guards
    action, _ = fill_sync_decision(
        last_sync=NOW - timedelta(hours=70), now=NOW, ledger_nonempty=True, history_supported=True, settings=settings
    )
    assert action == "fills"


def test_fill_window_at_window_minus_margin_backfills_from_history() -> None:
    settings = make_settings().guards  # 72h - 1h
    action, _ = fill_sync_decision(
        last_sync=NOW - timedelta(hours=71), now=NOW, ledger_nonempty=True, history_supported=True, settings=settings
    )
    assert action == "history"


def test_fill_window_margin_is_configurable() -> None:
    settings = make_settings(guards={"fill_window_hours": 72, "fill_safety_margin_hours": 12}).guards
    action, _ = fill_sync_decision(
        last_sync=NOW - timedelta(hours=61), now=NOW, ledger_nonempty=True, history_supported=True, settings=settings
    )
    assert action == "history"


def test_fill_gap_without_history_support_refuses() -> None:
    settings = make_settings().guards
    action, reason = fill_sync_decision(
        last_sync=NOW - timedelta(hours=80), now=NOW, ledger_nonempty=True, history_supported=False, settings=settings
    )
    assert action == "refuse"
    assert "fill_gap" in reason


def test_fill_gap_with_backfill_disabled_refuses() -> None:
    settings = make_settings(guards={"fill_history_backfill": False}).guards
    action, _ = fill_sync_decision(
        last_sync=NOW - timedelta(hours=80), now=NOW, ledger_nonempty=True, history_supported=True, settings=settings
    )
    assert action == "refuse"


def test_fill_gap_beyond_history_window_refuses() -> None:
    settings = make_settings().guards  # history 90d - 1d
    action, reason = fill_sync_decision(
        last_sync=NOW - timedelta(days=89, hours=1), now=NOW, ledger_nonempty=True, history_supported=True,
        settings=settings,
    )
    assert action == "refuse"
    assert "fills-history" in reason


def test_missing_sync_time_on_nonempty_ledger_refuses() -> None:
    settings = make_settings().guards
    action, reason = fill_sync_decision(
        last_sync=None, now=NOW, ledger_nonempty=True, history_supported=True, settings=settings
    )
    assert action == "refuse"
    assert "missing" in reason


def test_missing_sync_time_on_empty_ledger_is_a_fresh_start() -> None:
    settings = make_settings().guards
    action, _ = fill_sync_decision(
        last_sync=None, now=NOW, ledger_nonempty=False, history_supported=True, settings=settings
    )
    assert action == "fills"


# ---------- Guard 1: engine wiring ----------


def test_engine_refuses_and_trips_kill_switch_on_unrecoverable_gap(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive(history=False)
    alerter = MemoryAlerter()
    engine = _engine(tmp_path, ex, settings, alerter)
    engine.state.set_kv("fill_cursor", "110")
    engine.state.set_kv(FILL_SYNC_TS_KEY, (NOW - timedelta(hours=80)).isoformat())
    with pytest.raises(FillGapError):
        engine.sync_exchange_fills(now=NOW)
    assert "fills" not in ex.calls and "fills-history" not in ex.calls
    ks = KillSwitch(settings.kill_switch_path)
    assert ks.is_tripped and ks.reason.startswith("fill_gap")
    assert any(e.level == "critical" and "fill_gap" in e.message for e in alerter.events)


def test_engine_refuses_missing_sync_time_with_existing_cursor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive()
    engine = _engine(tmp_path, ex, settings)
    engine.state.set_kv("fill_cursor", "110")
    with pytest.raises(FillGapError):
        engine.sync_exchange_fills(now=NOW)
    assert ex.calls == []
    assert KillSwitch(settings.kill_switch_path).is_tripped


def test_engine_backfills_from_history_on_stale_sync(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive()
    engine = _engine(tmp_path, ex, settings)
    engine.state.set_kv("fill_cursor", "110")
    engine.state.set_kv(FILL_SYNC_TS_KEY, (NOW - timedelta(hours=75)).isoformat())
    engine.sync_exchange_fills(now=NOW)
    assert ex.calls == ["fills-history"]
    assert not KillSwitch(settings.kill_switch_path).is_tripped


def test_sync_time_is_persisted_only_with_the_book(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive()
    engine = _engine(tmp_path, ex, settings)
    engine.sync_exchange_fills(now=NOW)
    assert engine.state.get_kv(FILL_SYNC_TS_KEY) is None
    engine._persist()
    assert engine.state.get_kv(FILL_SYNC_TS_KEY) == NOW.isoformat()


def test_paper_exchange_without_cursor_is_not_gated(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = StubExchange()
    engine = _engine(tmp_path, ex, settings)
    engine.sync_exchange_fills(now=NOW)  # no fill cursor API → paper path, unchanged
    assert not KillSwitch(settings.kill_switch_path).is_tripped


def test_okx_history_sync_pages_fills_history_with_before_cursor() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append((request.url.path, params))
        rows: list[dict[str, str]] = []
        if request.url.path.endswith("/trade/fills-history") and params.get("before") == "110":
            rows = [
                {
                    "billId": "120", "tradeId": "gap-1", "clOrdId": "c", "instId": "BTC-USDT", "side": "buy",
                    "fillPx": "50000", "fillSz": "0.01", "fee": "-0.5", "feeCcy": "USDT", "ts": "1762920000000",
                }
            ]
        return httpx.Response(200, json={"code": "0", "data": rows})

    class _Limiter:
        def acquire(self) -> None:
            return None

    client = OkxClient(
        api_key="k", secret="s", passphrase="p", transport=httpx.MockTransport(handler), limiter=_Limiter()
    )
    ex = OkxExchange(client, min_notional=D("1"))
    ex.load_fill_cursor("110")
    ex.sync_history()
    fills = ex.pop_new_fills()
    assert [f.trade_id for f in fills] == ["gap-1"]
    assert ex.fill_cursor == "120"
    assert seen[0][0] == "/api/v5/trade/fills-history"
    assert seen[0][1].get("before") == "110"
    client.close()


def test_okx_get_balances_reads_cash_balance_per_ccy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v5/account/balance"
        assert request.url.params.get("ccy") == "BTC,USDT"
        details = [
            {"ccy": "BTC", "cashBal": "0.01", "availBal": "0.004", "frozenBal": "0.006"},
            {"ccy": "USDT", "cashBal": "7288", "availBal": "7288", "frozenBal": "0"},
        ]
        return httpx.Response(200, json={"code": "0", "data": [{"totalEq": "8000", "details": details}]})

    class _Limiter:
        def acquire(self) -> None:
            return None

    client = OkxClient(
        api_key="k", secret="s", passphrase="p", transport=httpx.MockTransport(handler), limiter=_Limiter()
    )
    ex = OkxExchange(client, min_notional=D("1"))
    assert ex.get_balances(["BTC", "USDT"]) == {"BTC": D("0.01"), "USDT": D("7288")}
    client.close()


def test_ack_fill_gap_cli_records_operator_sync_time(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    config = write_config(tmp_path, state={"sqlite_path": str(db)}, kill_switch_path=str(tmp_path / "KILL"))
    assert main(["ack-fill-gap", "--config", str(config)]) == 0
    raw = StateStore(db).get_kv(FILL_SYNC_TS_KEY)
    assert raw is not None
    assert datetime.fromisoformat(raw) > datetime.now(UTC) - timedelta(minutes=1)


# ---------- Guard 2: position mismatch ----------


def _seed_sync(engine: TradingEngine) -> None:
    engine.state.set_kv(FILL_SYNC_TS_KEY, datetime.now(UTC).isoformat())


def _book_with_btc(qty: str = "0.01") -> PortfolioBook:
    book = PortfolioBook(D("1000"))
    book.apply_fill(_buy(qty))
    return book


def test_mismatch_beyond_tolerance_is_reported() -> None:
    settings = make_settings(guards={"position_tolerance": {"BTC": "0.0001"}}).guards
    out = position_mismatches(
        _book_with_btc(), {"BTC": D("0.0098"), "USDT": D("500")}, symbols=["BTC-USDT"], quote_ccy="USDT",
        settings=settings,
    )
    assert len(out) == 1 and out[0].startswith("BTC")


def test_mismatch_within_tolerance_is_ok() -> None:
    settings = make_settings(guards={"position_tolerance": {"BTC": "0.0001"}}).guards
    out = position_mismatches(
        _book_with_btc(), {"BTC": D("0.00995"), "USDT": D("500")}, symbols=["BTC-USDT"], quote_ccy="USDT",
        settings=settings,
    )
    assert out == []


def test_flat_ledger_with_unexpected_base_balance_is_a_mismatch() -> None:
    settings = make_settings().guards
    out = position_mismatches(
        PortfolioBook(D("1000")), {"BTC": D("0.5"), "USDT": D("1000")}, symbols=["BTC-USDT"], quote_ccy="USDT",
        settings=settings,
    )
    assert out and out[0].startswith("BTC")


def test_non_bot_quote_balance_is_not_drift() -> None:
    # Wedding money (or any non-bot USDT) above the bot's allocation must never count as drift.
    settings = make_settings().guards
    out = position_mismatches(
        _book_with_btc(), {"BTC": D("0.01"), "USDT": D("7288")}, symbols=["BTC-USDT"], quote_ccy="USDT",
        settings=settings,
    )
    assert out == []


def test_quote_shortfall_below_bot_cash_is_drift() -> None:
    settings = make_settings().guards  # bot cash = 1000 - 500 = 500, tolerance 1
    out = position_mismatches(
        _book_with_btc(), {"BTC": D("0.01"), "USDT": D("480")}, symbols=["BTC-USDT"], quote_ccy="USDT",
        settings=settings,
    )
    assert out and out[0].startswith("USDT")


def test_engine_mismatch_trips_kill_switch_alerts_and_does_not_flatten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive(balances={"BTC": D("0"), "USDT": D("1000")})
    alerter = MemoryAlerter()
    engine = _engine(tmp_path, ex, settings, alerter)
    _seed_sync(engine)
    engine.book.apply_fill(_buy())
    assert engine.check_positions() is False
    ks = KillSwitch(settings.kill_switch_path)
    assert ks.is_tripped and ks.reason.startswith("position_mismatch") and ks.cancel_open
    assert any(e.level == "critical" and e.event == "kill_switch" for e in alerter.events)
    # Untrusted ledger: halt new orders and cancel resting ones, but never sell from a wrong book.
    engine.apply_kill_switch()
    engine.force_flatten(ks.reason)
    assert ex.placed == []
    assert "BTC-USDT" in ex.cancelled


def test_engine_match_within_tolerance_keeps_trading(tmp_path: Path) -> None:
    settings = _settings(tmp_path, position_tolerance={"BTC": "0.00001"})
    ex = FakeLive(balances={"BTC": D("0.010005"), "USDT": D("7288")})
    engine = _engine(tmp_path, ex, settings)
    engine.book.apply_fill(_buy())
    assert engine.check_positions() is True
    assert not KillSwitch(settings.kill_switch_path).is_tripped


def test_mismatch_halt_persists_across_restart_until_manual_reset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive(balances={"BTC": D("0"), "USDT": D("1000")})
    engine = _engine(tmp_path, ex, settings)
    _seed_sync(engine)
    engine.book.apply_fill(_buy())
    engine._persist()
    engine.check_positions()
    engine.state.close()
    # Restart: still tripped, start refused, and no auto-flatten on build.
    with pytest.raises(LiveGuardError):
        assert_start_allowed(settings)
    ex2 = FakeLive(balances={"BTC": D("0.01"), "USDT": D("1000")})
    engine2 = _engine(tmp_path, ex2, settings)
    engine2.force_flatten(KillSwitch(settings.kill_switch_path).reason)
    assert ex2.placed == []
    # Manual reset: operator reconciles, re-arms; the next check passes and stays armed.
    KillSwitch(settings.kill_switch_path).rearm(operator="andrew")
    assert_start_allowed(settings)
    assert engine2.check_positions() is True
    assert not KillSwitch(settings.kill_switch_path).is_tripped


def test_rearm_with_mismatch_still_present_trips_again(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive(balances={"BTC": D("0"), "USDT": D("1000")})
    engine = _engine(tmp_path, ex, settings)
    _seed_sync(engine)
    engine.book.apply_fill(_buy())
    engine.check_positions()
    KillSwitch(settings.kill_switch_path).rearm(operator="andrew")
    assert engine.check_positions() is False
    assert KillSwitch(settings.kill_switch_path).is_tripped


def test_balance_fetch_failure_does_not_pass_silently(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive()

    def boom(ccys: list[str]) -> dict[str, D]:
        raise RuntimeError("timeout")

    ex.get_balances = boom  # type: ignore[method-assign]
    alerter = MemoryAlerter()
    engine = _engine(tmp_path, ex, settings, alerter)
    assert engine.check_positions() is False
    assert any(e.level == "warning" and "position check" in e.message for e in alerter.events)


# ---------- ledger: base-currency fees ----------


def test_buy_with_base_currency_fee_books_net_qty() -> None:
    # OKX spot buys charge the fee in the base coin; the wallet holds fillSz - fee.
    book = PortfolioBook(D("1000"))
    book.apply_fill(_buy("0.01", fee="0.00001", fee_ccy="BTC"))
    pos = book.positions["BTC-USDT"]
    assert pos.qty == D("0.00999")
    assert pos.qty * pos.avg_price == D("500")


# ---------- review follow-ups ----------


def test_transient_mismatch_resolved_by_resync_does_not_trip(tmp_path: Path) -> None:
    # A fill lands between the fill sync and the balance read: re-sync once before halting.
    settings = _settings(tmp_path)
    ex = FakeLive(balances={"BTC": D("0.01"), "USDT": D("500")})
    pending = [_buy()]

    def sync() -> None:
        ex.calls.append("fills")
        ex._fills.extend(pending)
        pending.clear()

    ex.sync = sync  # type: ignore[method-assign]
    engine = _engine(tmp_path, ex, settings)
    assert engine.check_positions(now=NOW) is True
    assert "BTC-USDT" in engine.book.positions
    assert not KillSwitch(settings.kill_switch_path).is_tripped


def test_history_page_cap_keeps_sync_time_stale(tmp_path: Path) -> None:
    # If fills-history paging stopped at its page cap, the gap is not closed yet.
    settings = _settings(tmp_path)
    ex = FakeLive()
    ex.last_sync_complete = False  # type: ignore[attr-defined]
    engine = _engine(tmp_path, ex, settings)
    engine.state.set_kv("fill_cursor", "110")
    old = (NOW - timedelta(hours=75)).isoformat()
    engine.state.set_kv(FILL_SYNC_TS_KEY, old)
    engine.sync_exchange_fills(now=NOW)
    engine._persist()
    assert engine.state.get_kv(FILL_SYNC_TS_KEY) == old


def test_repeated_balance_failures_trip_kill_switch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, position_check_max_failures=3)
    ex = FakeLive()

    def boom(ccys: list[str]) -> dict[str, D]:
        raise RuntimeError("timeout")

    ex.get_balances = boom  # type: ignore[method-assign]
    engine = _engine(tmp_path, ex, settings)
    engine.check_positions()
    engine.check_positions()
    assert not KillSwitch(settings.kill_switch_path).is_tripped
    engine.check_positions()
    ks = KillSwitch(settings.kill_switch_path)
    assert ks.is_tripped and ks.reason.startswith("position_mismatch")


def test_start_refuses_when_positions_cannot_be_verified(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive()

    def boom(ccys: list[str]) -> dict[str, D]:
        raise RuntimeError("timeout")

    ex.get_balances = boom  # type: ignore[method-assign]
    engine = _engine(tmp_path, ex, settings)
    with pytest.raises(LiveGuardError):
        engine.assert_positions_at_start()


def test_tripped_mismatch_does_not_re_alert_every_poll(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ex = FakeLive(balances={"BTC": D("0"), "USDT": D("1000")})
    alerter = MemoryAlerter()
    engine = _engine(tmp_path, ex, settings, alerter)
    _seed_sync(engine)
    engine.book.apply_fill(_buy())
    engine.check_positions()
    count = len(alerter.events)
    assert engine.check_positions() is False
    assert len(alerter.events) == count
