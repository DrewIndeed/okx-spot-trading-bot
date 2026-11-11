from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from okxspot.errors import OkxSpotError
from okxspot.killswitch import KillSwitch

# Official spec_v2 paper runner. This supervisor must never signal or restart it.
PROTECTED_PID = 129703
PROTECTED_CMDLINE_MARKERS = ("/workspace/trading-bot/",)
LIVE_OR_RESEARCH_MARKERS = ("/workspace/trading-bot-live", "/workspace/trading-bot-v3")


class SupervisorError(OkxSpotError):
    """Supervisor refused to start, or tripped the restart cap."""


class _Alerter(Protocol):
    def send(self, level: str, message: str, event: str | None = None) -> None: ...


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
        proc = self._popen(self.argv, cwd=str(self.cwd) if self.cwd else None)
        pid = int(getattr(proc, "pid", 0) or 0)
        cmdline = " ".join(self.argv)
        if pid in self._protected or is_protected_pid(pid, cmdline):
            raise SupervisorError(f"refusing to manage protected pid {pid}")
        self.child = proc
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
        self.heartbeat_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _restarts_in_window(self, window: float = 3600.0) -> int:
        cutoff = self._now() - window
        self._restart_at = [t for t in self._restart_at if t >= cutoff]
        return len(self._restart_at)

    def run_until_stopped(self, max_cycles: int | None = None) -> None:
        self.spawn()
        cycles = 0
        while not self._stop:
            cycles += 1
            if max_cycles is not None and cycles > max_cycles:
                return
            rc = self.child.poll() if self.child is not None else 0
            if rc is None:
                self.write_heartbeat()
                self._sleeper(self.poll_seconds)
                continue
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
