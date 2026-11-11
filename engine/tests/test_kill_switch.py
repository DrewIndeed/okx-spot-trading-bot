from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from okxspot.killswitch import KillSwitch, KillSwitchError


def test_trip_blocks_new_orders_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    assert ks.is_tripped is False
    ks.trip("manual", cancel_open=False, at=datetime(2026, 11, 11, 10, 0, tzinfo=UTC))
    assert ks.is_tripped is True
    assert ks.reason == "manual"
    assert ks.cancel_open is False

    restarted = KillSwitch(path)
    assert restarted.is_tripped is True
    assert restarted.reason == "manual"
    assert restarted.allows_new_orders() is False


def test_restart_does_not_rearm(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    KillSwitch(path).trip("watchdog", cancel_open=True)
    again = KillSwitch(path)
    assert again.is_tripped is True
    assert again.cancel_open is True
    with pytest.raises(KillSwitchError, match="manual re-arm"):
        again.rearm_if_stale()
    assert again.is_tripped is True


def test_manual_rearm_only(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    ks.trip("operator")
    ks.rearm(operator="cli")
    assert ks.is_tripped is False
    assert ks.allows_new_orders() is True
    assert KillSwitch(path).is_tripped is False
    assert path.exists()
    assert "rearmed" in path.read_text(encoding="utf-8")


def test_trip_invokes_optional_cancel(tmp_path: Path) -> None:
    cancelled: list[str] = []
    ks = KillSwitch(tmp_path / "KILL_SWITCH", on_cancel_open=lambda: cancelled.append("yes"))
    ks.trip("risk", cancel_open=True)
    assert cancelled == ["yes"]
    ks2 = KillSwitch(tmp_path / "KILL_SWITCH", on_cancel_open=lambda: cancelled.append("again"))
    assert ks2.is_tripped is True
    # loading a tripped switch must not cancel again
    assert cancelled == ["yes"]
