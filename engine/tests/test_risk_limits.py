from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from okxspot.killswitch import KillSwitch
from okxspot.models import OrderPurpose, Side
from okxspot.risk.manager import RiskManager
from tests.conftest import instrument, make_settings

D = Decimal


def _mgr(**risk_over: object) -> RiskManager:
    settings = make_settings(risk=risk_over) if risk_over else make_settings()
    return RiskManager(settings.risk)


def _entry(mgr: RiskManager, **kwargs: object):
    params = dict(
        entry_price=D("100"),
        stop_price=D("99"),
        allocated_capital=D("1000"),
        realized_pnl=D("0"),
        available_quote=D("1000"),
        open_positions=0,
        current_exposure_notional=D("0"),
        instrument=instrument(),
        has_position=False,
        fee_rate=D("0"),
    )
    params.update(kwargs)
    return mgr.evaluate_entry(**params)  # type: ignore[arg-type]


def test_spot_long_only_rejects_short_entry_margin_and_leverage() -> None:
    mgr = _mgr()
    short = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.SELL,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        td_mode="cash",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert short.allowed is False
    assert short.reason == "spot_long_only"
    margin = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        td_mode="cross",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert margin.allowed is False
    assert margin.reason == "no_margin"
    lev = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        td_mode="cash",
        leverage=D("2"),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert lev.allowed is False
    assert lev.reason == "no_leverage"


def test_min_and_max_notional() -> None:
    mgr = RiskManager(make_settings(risk={"min_notional": "20", "max_notional": "80"}).risk)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    low = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("10"),
        now=now,
    )
    assert low.allowed is False and low.reason == "min_notional"
    high = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("100"),
        now=now,
    )
    assert high.allowed is False and high.reason == "max_notional"
    ok = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        now=now,
    )
    assert ok.allowed is True


def test_max_orders_per_minute_and_per_day() -> None:
    mgr = RiskManager(make_settings(risk={"max_orders_per_minute": 2, "max_orders_per_day": 3}).risk)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for i in range(2):
        d = mgr.pretrade(
            inst_id="BTC-USDT",
            side=Side.BUY,
            purpose=OrderPurpose.ENTRY,
            notional=D("50"),
            now=t0 + timedelta(seconds=i),
        )
        assert d.allowed is True
    blocked = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        now=t0 + timedelta(seconds=10),
    )
    assert blocked.allowed is False
    assert blocked.reason == "max_orders_per_minute"
    # next minute, daily cap still has one slot then blocks
    third = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        now=t0 + timedelta(seconds=61),
    )
    assert third.allowed is True
    fourth = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        notional=D("50"),
        now=t0 + timedelta(seconds=90),
    )
    assert fourth.allowed is False
    assert fourth.reason == "max_orders_per_day"


def test_daily_loss_is_utc_day_and_blocks_entries_not_exits() -> None:
    mgr = _mgr()
    start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    mgr.on_equity(D("1000"), start)
    mgr.on_equity(D("960"), start + timedelta(minutes=10))  # 4% > 3%
    denied = _entry(mgr)
    assert denied.allowed is False
    assert denied.reason == "daily_loss_limit"
    exit_ok = mgr.pretrade(
        inst_id="BTC-USDT",
        side=Side.SELL,
        purpose=OrderPurpose.EXIT,
        notional=D("50"),
        now=start + timedelta(minutes=11),
    )
    assert exit_ok.allowed is True
    # next UTC day resets
    nxt = datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
    mgr.on_equity(D("960"), nxt)
    allowed = _entry(mgr)
    assert allowed.allowed is True


def test_file_kill_switch_blocks_entries_in_pretrade(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    mgr = RiskManager(make_settings().risk, kill_switch=ks)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert mgr.pretrade(
        inst_id="BTC-USDT", side=Side.BUY, purpose=OrderPurpose.ENTRY, notional=D("50"), now=now
    ).allowed is True
    ks.trip("cli")
    blocked = mgr.pretrade(
        inst_id="BTC-USDT", side=Side.BUY, purpose=OrderPurpose.ENTRY, notional=D("50"), now=now
    )
    assert blocked.allowed is False
    assert blocked.reason == "kill_switch"
