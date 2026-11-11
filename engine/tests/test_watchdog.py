from __future__ import annotations

import json
from pathlib import Path

import pytest

from okxspot.alerts import MemoryAlerter
from okxspot.killswitch import KillSwitch
from okxspot.watchdog import (
    PROTECTED_PID,
    Supervisor,
    SupervisorError,
    is_protected_pid,
)


class FakeProc:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
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
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_heartbeat_is_written(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat.json"
    ks = KillSwitch(tmp_path / "KILL")
    alerts = MemoryAlerter()
    clock = {"t": 1_700_000_000.0}

    def popen(*_a, **_k):
        return FakeProc(4242, returncode=None)

    sup = Supervisor(
        ["python", "-m", "okxspot", "paper", "--once"],
        heartbeat_path=hb,
        kill_switch=ks,
        alerter=alerts,
        now=lambda: clock["t"],
        popen=popen,
        sleeper=lambda _s: None,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    sup.spawn()
    sup.write_heartbeat()
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["running"] is True
    assert data["child_pid"] != PROTECTED_PID


def test_max_restarts_per_hour_trips_kill_switch_and_alerts(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    alerts = MemoryAlerter()
    clock = {"t": 1_700_000_000.0}
    launches: list[int] = []

    def popen(*_a, **_k):
        launches.append(1)
        return FakeProc(5000 + len(launches), returncode=1)

    sup = Supervisor(
        ["python", "-c", "raise SystemExit(1)"],
        heartbeat_path=tmp_path / "hb.json",
        kill_switch=ks,
        alerter=alerts,
        max_restarts_per_hour=3,
        backoff_seconds=(0, 0, 0),
        now=lambda: clock["t"],
        popen=popen,
        sleeper=lambda _s: None,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    with pytest.raises(SupervisorError, match="max_restarts_per_hour"):
        sup.run_until_stopped(max_cycles=10)
    assert ks.is_tripped is True
    assert ks.reason == "max_restarts_per_hour"
    assert any(e.event == "restart" for e in alerts.events)
    assert any(e.event == "kill_switch" for e in alerts.events)
    assert len(launches) == 4  # initial + 3 restarts, then trip


def test_refuses_to_manage_protected_official_pid() -> None:
    assert is_protected_pid(PROTECTED_PID) is True
    assert is_protected_pid(PROTECTED_PID, cmdline="/workspace/trading-bot/.venv/bin/python -m tradingbot paper-loop")
    assert is_protected_pid(12, cmdline="/workspace/trading-bot-live/.venv/bin/python -m tradingbot paper-loop") is False
    with pytest.raises(SupervisorError, match="protected"):
        Supervisor.ensure_not_protected(PROTECTED_PID, cmdline="paper-loop")


def test_supervisor_unit_and_script_do_not_target_official_runner() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "supervise-paper.sh").read_text(encoding="utf-8")
    unit = (root / "deploy" / "okxspot-paper.service").read_text(encoding="utf-8")
    assert "/workspace/trading-bot-live" in script
    assert "129703" in script
    assert "/workspace/trading-bot-live" in unit
    assert "/workspace/trading-bot/" not in unit.replace("/workspace/trading-bot-live", "")
    assert "129703" in unit
