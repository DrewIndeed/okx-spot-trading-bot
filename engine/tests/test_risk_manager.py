from __future__ import annotations

from decimal import Decimal

from okxspot.risk.manager import RiskManager
from tests.conftest import day, instrument, make_settings

D = Decimal


def _risk(**overrides: object) -> RiskManager:
    calls: list[str] = []
    manager = RiskManager(
        make_settings(**overrides).risk,
        on_flatten=calls.append,
    )
    manager._calls = calls  # type: ignore[attr-defined]
    return manager


def _entry(manager: RiskManager, **overrides: object) -> object:
    params: dict[str, object] = {
        "entry_price": D("100"),
        "stop_price": D("95"),
        "allocated_capital": D("1000"),
        "realized_pnl": D("0"),
        "available_quote": D("1000"),
        "open_positions": 0,
        "current_exposure_notional": D("0"),
        "instrument": instrument(),
        "has_position": False,
        "fee_rate": D("0"),
    }
    params.update(overrides)
    return manager.evaluate_entry(**params)  # type: ignore[arg-type]


def test_per_coin_cap_binds() -> None:
    manager = _risk(risk={"per_coin_cap_pct": "0.25"})
    decision = _entry(manager, stop_price=D("99.5"))
    assert decision.allowed  # type: ignore[attr-defined]
    assert decision.qty == D("2.5")  # type: ignore[attr-defined]


def test_total_exposure_cap_binds() -> None:
    manager = _risk(risk={"max_total_exposure_pct": "0.5", "per_coin_cap_pct": "1"})
    decision = _entry(
        manager,
        stop_price=D("99"),
        current_exposure_notional=D("400"),
    )
    assert decision.qty == D("1")  # type: ignore[attr-defined]


def test_exposure_already_full_rejects() -> None:
    manager = _risk(risk={"max_total_exposure_pct": "0.5"})
    decision = _entry(manager, current_exposure_notional=D("500"))
    assert decision.allowed is False  # type: ignore[attr-defined]
    assert decision.reason == "max_total_exposure"  # type: ignore[attr-defined]


def test_max_concurrent_positions() -> None:
    manager = _risk()
    decision = _entry(manager, open_positions=3)
    assert decision.reason == "max_concurrent_positions"  # type: ignore[attr-defined]


def test_existing_position_rejects_add() -> None:
    decision = _entry(_risk(), has_position=True)
    assert decision.reason == "position_open"  # type: ignore[attr-defined]


def test_invalid_stop_rejected() -> None:
    decision = _entry(_risk(), stop_price=D("100"))
    assert decision.reason == "invalid_stop"  # type: ignore[attr-defined]
    decision = _entry(_risk(), stop_price=None)
    assert decision.reason == "invalid_stop"  # type: ignore[attr-defined]


def test_below_lot_and_min_notional() -> None:
    tiny = _entry(
        _risk(),
        allocated_capital=D("1000"),
        available_quote=D("10"),
        entry_price=D("200"),
        stop_price=D("100"),
        instrument=instrument(lot_sz=D("0.01"), min_sz=D("0.01")),
    )
    assert tiny.reason == "below_lot_size"  # type: ignore[attr-defined]
    notional = _entry(
        _risk(),
        allocated_capital=D("100"),
        available_quote=D("100"),
        entry_price=D("10"),
        stop_price=D("5"),
        instrument=instrument(lot_sz=D("0.001"), min_sz=D("0.001"), min_notional=D("1000")),
    )
    assert notional.reason == "min_notional"  # type: ignore[attr-defined]


def test_daily_loss_blocks_entries_without_flatten() -> None:
    manager = _risk()
    manager.on_equity(D("1000"), day(0))
    manager.on_equity(D("960"), day(0))
    assert manager.daily_loss_hit
    assert manager.halted is False
    assert manager._calls == []  # type: ignore[attr-defined]
    decision = _entry(manager)
    assert decision.reason == "daily_loss_limit"  # type: ignore[attr-defined]
    manager.on_equity(D("960"), day(1))
    assert manager.daily_loss_hit is False


def test_kill_switch_flattens_once_and_halts() -> None:
    manager = _risk()
    manager.on_equity(D("1000"), day(0))
    manager.on_equity(D("1100"), day(0))
    manager.on_equity(D("900"), day(0))
    assert manager.halted
    assert manager.halt_reason == "max_drawdown"
    assert manager._calls == ["max_drawdown"]  # type: ignore[attr-defined]
    manager.on_equity(D("800"), day(0))
    assert manager._calls == ["max_drawdown"]  # type: ignore[attr-defined]
    decision = _entry(manager)
    assert decision.reason == "halted"  # type: ignore[attr-defined]


def test_loss_cooldown_blocks_n_bars() -> None:
    manager = _risk()
    manager.register_close(D("5"))
    assert _entry(manager).allowed  # type: ignore[attr-defined]
    manager.register_close(D("-1"))
    assert _entry(manager).reason == "loss_cooldown"  # type: ignore[attr-defined]
    manager.on_new_bar()
    assert _entry(manager).reason == "loss_cooldown"  # type: ignore[attr-defined]
    manager.on_new_bar()
    assert _entry(manager).reason == "loss_cooldown"  # type: ignore[attr-defined]
    manager.on_new_bar()
    assert _entry(manager).allowed  # type: ignore[attr-defined]


def test_risk_state_roundtrip_keeps_halt() -> None:
    manager = _risk()
    manager.on_equity(D("1000"), day(0))
    manager.on_equity(D("800"), day(0))
    assert manager.halted
    restored = _risk()
    restored.load_json(manager.export_json())
    assert restored.halted
    assert restored.halt_reason == "max_drawdown"
    assert restored.flatten_sent
    assert _entry(restored).reason == "halted"  # type: ignore[attr-defined]


def test_stop_tick_round_does_not_exceed_risk_budget() -> None:
    manager = _risk()
    decision = _entry(
        manager,
        entry_price=D("100"),
        stop_price=D("95.016"),
        instrument=instrument(tick_sz=D("0.01"), lot_sz=D("0.001"), min_sz=D("0.001")),
    )
    assert decision.allowed  # type: ignore[attr-defined]
    assert decision.stop_price == D("95.01")  # type: ignore[attr-defined]
    assert decision.risk_amount <= D("10") + D("0.0000001")  # type: ignore[attr-defined]
