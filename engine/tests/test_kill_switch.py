from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from okxspot.killswitch import KillSwitch, KillSwitchError


def test_trip_blocks_new_orders_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
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
    setup = KillSwitch(path)
    setup.rearm(operator="setup")
    setup.trip("watchdog", cancel_open=True)
    again = KillSwitch(path)
    assert again.is_tripped is True
    assert again.cancel_open is True
    with pytest.raises(KillSwitchError, match="manual re-arm"):
        again.rearm_if_stale()
    assert again.is_tripped is True


def test_manual_rearm_only(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
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


def test_b2_missing_file_is_tripped(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    assert not path.exists()
    ks = KillSwitch(path)
    assert ks.is_tripped is True
    assert ks.allows_new_orders() is False


def test_b2_delete_after_trip_stays_tripped(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
    assert ks.is_tripped is False
    ks.trip("manual")
    path.unlink()
    again = KillSwitch(path)
    assert again.is_tripped is True
    assert again.allows_new_orders() is False


def test_b2_nondict_or_truncated_is_not_clear(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    path.write_text("[]", encoding="utf-8")
    assert KillSwitch(path).is_tripped is not False
    path.write_text("{", encoding="utf-8")
    assert KillSwitch(path).is_tripped is not False
    path.write_text("true", encoding="utf-8")
    assert KillSwitch(path).is_tripped is not False


def test_b2_partial_write_does_not_lose_tripped_state(tmp_path: Path) -> None:
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    ks.rearm(operator="setup")
    ks.trip("persist-me")
    # Simulate a crash mid-write: a sibling tmp is truncated; the real file must stay valid
    # or the switch must fail closed — never report clear.
    junk = path.with_name(path.name + ".tmp")
    junk.write_text('{"tripped":', encoding="utf-8")
    again = KillSwitch(path)
    assert again.is_tripped is True
    assert again.reason == "persist-me"
    # If the committed file itself is truncated, still fail closed.
    path.write_text('{"tripped":', encoding="utf-8")
    broken = KillSwitch(path)
    assert broken.is_tripped is not False
