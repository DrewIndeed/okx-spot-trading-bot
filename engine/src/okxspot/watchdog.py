from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from okxspot.atomic import write_text_atomic
from okxspot.errors import OkxSpotError
from okxspot.killswitch import KillSwitch
from okxspot.safety import SUPERVISED_ENV

# Official spec_v2 paper runner. This supervisor must never signal or restart it.
PROTECTED_PID = 129703
PROTECTED_CMDLINE_MARKERS = ("/workspace/trading-bot/",)
LIVE_OR_RESEARCH_MARKERS = ("/workspace/trading-bot-live", "/workspace/trading-bot-v3")


class SupervisorError(OkxSpotError):
    """Supervisor refused to start, or tripped the restart cap."""


class _Alerter(Protocol):
    def send(self, level: str, message: str, event: str | None = None) -> None: ...


class PauseDetector:
    """Detect a box/VM pause or a wall-clock step between two observations.

    CLOCK_MONOTONIC does not advance while the VM is paused (Firecracker
    snapshot/restore on this box), but the wall clock is stepped forward on
    resume. A heartbeat aged by wall clock then looks stale although the
    process was frozen with everything else. Returns the unexplained wall-clock
    jump in seconds (0.0 when below ``threshold_seconds``).
    """

    def __init__(
        self,
        *,
        wall: Callable[[], float],
        monotonic: Callable[[], float],
        threshold_seconds: float = 5.0,
    ) -> None:
        self._wall = wall
        self._mono = monotonic
        self.threshold_seconds = float(threshold_seconds)
        self._last: tuple[float, float] | None = None

    def check(self) -> float:
        wall, mono = self._wall(), self._mono()
        last, self._last = self._last, (wall, mono)
        if last is None:
            return 0.0
        jump = (wall - last[0]) - (mono - last[1])
        return jump if abs(jump) >= self.threshold_seconds else 0.0


def is_protected_pid(pid: int, cmdline: str | None = None) -> bool:
    if int(pid) == PROTECTED_PID:
        return True
    if not cmdline:
        return False
    if any(marker in cmdline for marker in LIVE_OR_RESEARCH_MARKERS):
        return False
    return any(marker in cmdline for marker in PROTECTED_CMDLINE_MARKERS)


def read_cmdline(pid: int) -> str:
    path = Path(f"/proc/{pid}/cmdline")
    if not path.exists():
        return ""
    return path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


class Supervisor:
    """Restart a paper/demo runner with backoff. Never touches the official v2 pid."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        heartbeat_path: str | Path,
        kill_switch: KillSwitch,
        alerter: _Alerter,
        max_restarts_per_hour: int = 5,
        backoff_seconds: Sequence[float] = (1.0, 5.0, 15.0, 30.0, 60.0),
        poll_seconds: float = 1.0,
        now: Callable[[], float] | None = None,
        popen: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] | None = None,
        protected_pids: frozenset[int] | None = None,
        cwd: str | Path | None = None,
        child_heartbeat_path: str | Path | None = None,
        child_stale_seconds: float = 90.0,
        monotonic: Callable[[], float] | None = None,
        child_start_grace_seconds: float | None = None,
        pause_threshold_seconds: float = 5.0,
        terminate_timeout_seconds: float = 10.0,
    ) -> None:
        if max_restarts_per_hour < 1:
            raise ValueError("max_restarts_per_hour must be >= 1")
        self.argv = list(argv)
        self.heartbeat_path = Path(heartbeat_path)
        self.kill_switch = kill_switch
        self.alerter = alerter
        self.max_restarts_per_hour = max_restarts_per_hour
        self.backoff_seconds = list(backoff_seconds) or [1.0]
        self.poll_seconds = poll_seconds
        self._now = now or (lambda: datetime.now(UTC).timestamp())
        self._popen = popen or subprocess.Popen
        self._sleeper = sleeper or _default_sleep
        self._protected = protected_pids if protected_pids is not None else frozenset({PROTECTED_PID})
        self.cwd = cwd
        self.child_heartbeat_path = Path(child_heartbeat_path) if child_heartbeat_path else None
        self.child_stale_seconds = float(child_stale_seconds)
        # With an injected wall clock and no monotonic one (older tests), use
        # the same source: no pause is ever simulated there.
        self._mono = monotonic or (self._now if now is not None else time.monotonic)
        self.child_start_grace_seconds = (
            float(child_start_grace_seconds) if child_start_grace_seconds is not None else self.child_stale_seconds
        )
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        self._pause = PauseDetector(wall=self._now, monotonic=self._mono, threshold_seconds=pause_threshold_seconds)
        self._grace_until = 0.0
        self.child: Any = None
        self._restart_at: list[float] = []
        self._backoff_index = 0
        self._stop = False

    @staticmethod
    def ensure_not_protected(pid: int, cmdline: str | None = None) -> None:
        text = cmdline if cmdline is not None else read_cmdline(pid)
        if int(pid) in {PROTECTED_PID} or is_protected_pid(pid, text):
            raise SupervisorError(f"refusing to manage protected pid {pid}")

    def spawn(self) -> None:
        if self.kill_switch.is_tripped:
            raise SupervisorError(f"kill switch is tripped ({self.kill_switch.reason})")
        env = os.environ.copy()
        env[SUPERVISED_ENV] = "1"
        proc = self._popen(self.argv, cwd=str(self.cwd) if self.cwd else None, env=env)
        pid = int(getattr(proc, "pid", 0) or 0)
        cmdline = " ".join(self.argv)
        if pid in self._protected or is_protected_pid(pid, cmdline):
            raise SupervisorError(f"refusing to manage protected pid {pid}")
        self.child = proc
        # A fresh child needs time to import and write its first beat; the
        # heartbeat file still holds the previous child's timestamp until then.
        self._grace_until = self._mono() + self.child_start_grace_seconds
        self.write_heartbeat()

    def write_heartbeat(self) -> None:
        pid = int(getattr(self.child, "pid", 0) or 0) if self.child is not None else os.getpid()
        running = bool(self.child is not None and getattr(self.child, "returncode", 0) is None)
        payload = {
            "ts": datetime.fromtimestamp(self._now(), tz=UTC).isoformat(),
            "pid": pid,
            "child_pid": pid,
            "running": running,
            "argv": self.argv,
            "supervisor_pid": os.getpid(),
        }
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.heartbeat_path, json.dumps(payload) + "\n")

    def _restarts_in_window(self, window: float = 3600.0) -> int:
        cutoff = self._now() - window
        self._restart_at = [t for t in self._restart_at if t >= cutoff]
        return len(self._restart_at)

    def _terminate_child(self) -> None:
        if self.child is None:
            return
        pid = int(getattr(self.child, "pid", 0) or 0)
        if pid and (pid in self._protected or is_protected_pid(pid, read_cmdline(pid))):
            return
        if hasattr(self.child, "terminate"):
            self.child.terminate()
        elif pid:
            os.kill(pid, signal.SIGTERM)
        # Reap before a replacement starts so two runners never overlap.
        waiter = getattr(self.child, "wait", None)
        if callable(waiter):
            try:
                waiter(timeout=self.terminate_timeout_seconds)
            except subprocess.TimeoutExpired:
                killer = getattr(self.child, "kill", None)
                if callable(killer):
                    killer()
                    waiter(timeout=self.terminate_timeout_seconds)

    def _note_pause(self) -> None:
        jump = self._pause.check()
        if jump:
            self._grace_until = max(self._grace_until, self._mono() + self.child_stale_seconds)
            self.alerter.send(
                "warning",
                f"box pause or wall-clock step of {jump:.0f}s detected; resyncing child heartbeat, not restarting",
                event="box_pause",
            )

    def _child_frozen(self) -> bool:
        if self.child_heartbeat_path is None or not self.child_heartbeat_path.exists():
            return False
        if self._mono() < self._grace_until:
            return False
        try:
            data = json.loads(self.child_heartbeat_path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(str(data.get("ts")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            beat_pid = int(data.get("pid") or 0)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return True
        child_pid = int(getattr(self.child, "pid", 0) or 0)
        if beat_pid and child_pid and beat_pid != child_pid:
            # Grace is over and the current child never wrote a beat: hung at start.
            return True
        age = self._now() - ts.timestamp()
        return age > self.child_stale_seconds

    def run_until_stopped(self, max_cycles: int | None = None) -> None:
        self.spawn()
        cycles = 0
        while not self._stop:
            cycles += 1
            if max_cycles is not None and cycles > max_cycles:
                return
            self._note_pause()
            self.kill_switch.reload()
            if self.kill_switch.is_tripped:
                self._terminate_child()
                self.alerter.send(
                    "critical",
                    f"kill switch tripped ({self.kill_switch.reason}); child stopped",
                    event="kill_switch",
                )
                raise SupervisorError("kill switch tripped")
            rc = self.child.poll() if self.child is not None else 0
            if rc is None:
                if self._child_frozen():
                    self.alerter.send("critical", "child heartbeat is stale; stopping child", event="restart")
                    self._terminate_child()
                    rc = self.child.poll() if self.child is not None else -1
                    if rc is None:
                        rc = -1
                    # fall through to restart accounting
                else:
                    self.write_heartbeat()
                    self._sleeper(self.poll_seconds)
                    continue
            if self.kill_switch.is_tripped:
                raise SupervisorError("kill switch tripped")
            if self._restarts_in_window() >= self.max_restarts_per_hour:
                self.kill_switch.trip("max_restarts_per_hour", source="watchdog")
                self.alerter.send(
                    "critical",
                    "max restarts per hour exceeded; kill switch tripped",
                    event="kill_switch",
                )
                raise SupervisorError("max_restarts_per_hour")
            self.alerter.send("warning", f"child exited rc={rc}; restarting", event="restart")
            delay = self.backoff_seconds[min(self._backoff_index, len(self.backoff_seconds) - 1)]
            self._backoff_index += 1
            if delay:
                self._sleeper(delay)
            self._restart_at.append(self._now())
            self.spawn()

    def request_stop(self) -> None:
        self._stop = True
        if self.child is None:
            return
        pid = int(getattr(self.child, "pid", 0) or 0)
        if pid and (pid in self._protected or is_protected_pid(pid)):
            return
        if hasattr(self.child, "terminate"):
            self.child.terminate()


def _default_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    import time

    time.sleep(seconds)


def monitor_heartbeat(
    *,
    heartbeat_path: str | Path,
    kill_switch: KillSwitch,
    max_age_seconds: float,
    protected_pids: frozenset[int] | None = None,
    now: Callable[[], float] | None = None,
) -> bool:
    """Fail closed if the supervisor heartbeat is stale. Never signals protected pids."""
    protected = protected_pids if protected_pids is not None else frozenset({PROTECTED_PID})
    path = Path(heartbeat_path)
    clock = now or (lambda: datetime.now(UTC).timestamp())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(str(data.get("ts")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = clock() - ts.timestamp()
        running = bool(data.get("running"))
        pid = int(data.get("child_pid") or data.get("pid") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        kill_switch.trip("stale_supervisor_heartbeat", source="monitor")
        return True
    if age <= max_age_seconds and running:
        return False
    if pid and pid not in protected and not is_protected_pid(pid, read_cmdline(pid) if pid else ""):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    kill_switch.trip("stale_supervisor_heartbeat", source="monitor")
    return True


def monitor_loop(
    *,
    heartbeat_path: str | Path,
    kill_switch: KillSwitch,
    max_age_seconds: float,
    poll_seconds: float = 15.0,
    protected_pids: frozenset[int] | None = None,
    now: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
    max_cycles: int | None = None,
    monotonic: Callable[[], float] | None = None,
    pause_threshold_seconds: float = 5.0,
) -> bool:
    """Fail-closed monitor loop. Returns True once the kill switch is tripped.

    After a detected box pause / wall-clock step the check is skipped once so
    the (also just-resumed) supervisor can refresh its heartbeat.
    """
    sleep = sleeper or _default_sleep
    wall = now or (lambda: datetime.now(UTC).timestamp())
    pause = PauseDetector(
        wall=wall,
        monotonic=monotonic or (wall if now is not None else time.monotonic),
        threshold_seconds=pause_threshold_seconds,
    )
    cycles = 0
    while True:
        cycles += 1
        if pause.check():
            if max_cycles is not None and cycles >= max_cycles:
                return False
            sleep(min(poll_seconds, max_age_seconds / 2))
            continue
        if monitor_heartbeat(
            heartbeat_path=heartbeat_path,
            kill_switch=kill_switch,
            max_age_seconds=max_age_seconds,
            protected_pids=protected_pids,
            now=now,
        ):
            return True
        if max_cycles is not None and cycles >= max_cycles:
            return False
        sleep(poll_seconds)
