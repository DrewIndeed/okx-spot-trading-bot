"""Soak hang 2026-11-11 18:22-18:25 (UTC+7): VM pause + restart storm.

Evidence (sofia-soak-hang-*.md): the box was paused ~65s; the wall clock jumped
on resume while the monotonic clock did not. The supervisor judged the healthy,
sleeping child stale by wall-clock age and killed it, then killed every fresh
child within ~30 ms because the heartbeat file still held the previous child's
timestamp. Five restarts later max_restarts_per_hour tripped the kill switch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from okxspot.alerts import MemoryAlerter
from okxspot.killswitch import KillSwitch
from okxspot.watchdog import PROTECTED_PID, Supervisor, monitor_loop

T0 = 1_700_000_000.0


class Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return self.returncode if self.returncode is not None else 0


def _hb(path: Path, ts: float, pid: int) -> None:
    path.write_text(
        json.dumps({"ts": datetime.fromtimestamp(ts, tz=UTC).isoformat(), "pid": pid, "running": True}),
        encoding="utf-8",
    )


class Box:
    """Fake wall + monotonic clocks. pause() advances wall only (VM pause)."""

    def __init__(self) -> None:
        self.wall = T0
        self.mono = 1000.0

    def tick(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def pause(self, seconds: float) -> None:
        self.wall += seconds


def _sup(tmp_path: Path, box: Box, procs: list[Proc], alerts: MemoryAlerter, **kw: object) -> Supervisor:
    ks = KillSwitch(tmp_path / "KILL")
    ks.rearm(operator="setup")
    pids = iter(range(9001, 9100))

    def popen(*_a: object, **_k: object) -> Proc:
        proc = Proc(next(pids))
        procs.append(proc)
        return proc

    options: dict[str, object] = {
        "heartbeat_path": tmp_path / "hb.json",
        "kill_switch": ks,
        "alerter": alerts,
        "child_heartbeat_path": tmp_path / "hb.child.json",
        "child_stale_seconds": 90,
        "now": lambda: box.wall,
        "monotonic": lambda: box.mono,
        "popen": popen,
        "poll_seconds": 1.0,
        "protected_pids": frozenset({PROTECTED_PID}),
    }
    options.update(kw)
    return Supervisor(["python", "-c", "pass"], **options)  # type: ignore[arg-type]


def test_restart_grace_ignores_previous_childs_heartbeat(tmp_path: Path) -> None:
    # Reproduces the storm: the heartbeat file still belongs to the killed child.
    box, procs, alerts = Box(), [], MemoryAlerter()
    child_hb = tmp_path / "hb.child.json"
    _hb(child_hb, T0 - 200, pid=356374)  # previous child's last beat
    sup = _sup(tmp_path, box, procs, alerts, sleeper=lambda s: box.tick(s))
    sup.run_until_stopped(max_cycles=30)  # 30 s of supervision after spawn
    assert len(procs) == 1
    assert procs[0].terminated is False
    assert not KillSwitch(tmp_path / "KILL").is_tripped


def test_new_child_that_never_heartbeats_is_restarted_after_grace(tmp_path: Path) -> None:
    box, procs, alerts = Box(), [], MemoryAlerter()
    _hb(tmp_path / "hb.child.json", T0 - 200, pid=356374)
    sup = _sup(tmp_path, box, procs, alerts, sleeper=lambda s: box.tick(s), max_restarts_per_hour=50)
    sup.run_until_stopped(max_cycles=95)
    assert procs[0].terminated is True
    assert procs[0].waited is True  # reaped before the replacement starts
    assert len(procs) == 2


def test_box_pause_wall_jump_is_resync_not_hang(tmp_path: Path) -> None:
    box, procs, alerts = Box(), [], MemoryAlerter()
    child_hb = tmp_path / "hb.child.json"
    state = {"n": 0}

    def sleeper(seconds: float) -> None:
        state["n"] += 1
        if state["n"] == 100:
            # VM paused 65 s after the child's beat at n=60 (40 s ago): wall
            # jumps, monotonic does not, so the child is still mid-sleep.
            box.pause(65)
        else:
            box.tick(seconds)
        if state["n"] % 60 == 0 or state["n"] == 1:
            _hb(child_hb, box.wall, pid=procs[-1].pid)

    sup = _sup(tmp_path, box, procs, alerts, sleeper=sleeper)
    sup.run_until_stopped(max_cycles=170)
    assert len(procs) == 1 and procs[0].terminated is False
    assert any(e.event == "box_pause" for e in alerts.events)
    assert not KillSwitch(tmp_path / "KILL").is_tripped


def test_genuine_hang_without_pause_still_restarts(tmp_path: Path) -> None:
    box, procs, alerts = Box(), [], MemoryAlerter()
    child_hb = tmp_path / "hb.child.json"
    state = {"n": 0}

    def sleeper(seconds: float) -> None:
        state["n"] += 1
        box.tick(seconds)
        if state["n"] == 1:
            _hb(child_hb, box.wall, pid=procs[0].pid)  # one beat, then hung

    sup = _sup(tmp_path, box, procs, alerts, sleeper=sleeper, max_restarts_per_hour=50)
    sup.run_until_stopped(max_cycles=100)
    assert procs[0].terminated is True
    assert not any(e.event == "box_pause" for e in alerts.events)


def test_monitor_loop_skips_one_check_after_box_pause(tmp_path: Path) -> None:
    box = Box()
    hb = tmp_path / "hb.json"
    ks = KillSwitch(tmp_path / "KILL")
    ks.rearm(operator="setup")
    _hb(hb, box.wall, pid=9001)
    state = {"n": 0}

    def sleeper(seconds: float) -> None:
        state["n"] += 1
        if state["n"] == 1:
            box.pause(300)  # heartbeat looks 300 s old right after resume
        else:
            box.tick(seconds)
            _hb(hb, box.wall, pid=9001)  # supervisor beats again once resumed

    tripped = monitor_loop(
        heartbeat_path=hb,
        kill_switch=ks,
        max_age_seconds=60,
        poll_seconds=15,
        now=lambda: box.wall,
        monotonic=lambda: box.mono,
        sleeper=sleeper,
        max_cycles=4,
        protected_pids=frozenset({PROTECTED_PID, 9001}),
    )
    assert tripped is False
    assert not ks.is_tripped


def test_short_grace_new_child_without_own_beat_is_hung(tmp_path: Path) -> None:
    # The previous child's beat is recent, but it is not this child's beat.
    box, procs, alerts = Box(), [], MemoryAlerter()
    _hb(tmp_path / "hb.child.json", T0 - 5, pid=356374)
    sup = _sup(
        tmp_path, box, procs, alerts, sleeper=lambda s: box.tick(s), child_start_grace_seconds=10,
        max_restarts_per_hour=50,
    )
    sup.run_until_stopped(max_cycles=15)
    assert procs[0].terminated is True
