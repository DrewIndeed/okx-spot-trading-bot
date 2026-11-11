"""24h paper loop helpers: pidfile, status, start/stop, and a single iteration.

The loop polls public REST closed candles only, runs configured variants, and
appends the paper-log schema + portfolio state. It never places live orders.
Do not start the 24h process unless explicitly asked; paper-once is the one-shot path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot.config import Config
from tradingbot.logutil import JsonlLogger
from tradingbot.portfolio import Portfolio

DEFAULT_PIDFILE = "state/paper.pid"
DEFAULT_STATUS = "state/paper_status.json"
DEFAULT_POLL_SEC = 60


@dataclass
class RunnerStatus:
    running: bool
    pid: Optional[int] = None
    pidfile: str = DEFAULT_PIDFILE
    stale: bool = False
    started_at: Optional[str] = None
    last_iteration_at: Optional[str] = None
    last_result: Optional[str] = None
    last_error: Optional[str] = None
    last_equity: Optional[float] = None
    poll_sec: Optional[int] = None

    def as_dict(self) -> dict:
        return asdict(self)


def pidfile_path(cfg: Optional[Config] = None, run_id: Optional[str] = None) -> Path:
    if run_id:
        return Path("state") / "spec_v2" / run_id / "paper.pid"
    raw = DEFAULT_PIDFILE
    if cfg and getattr(cfg, "state_path", None):
        raw = str(Path(cfg.state_path).parent / "paper.pid")
    return Path(raw)


def status_path(cfg: Optional[Config] = None, run_id: Optional[str] = None) -> Path:
    if run_id:
        return Path("state") / "spec_v2" / run_id / "paper_status.json"
    raw = DEFAULT_STATUS
    if cfg and getattr(cfg, "state_path", None):
        raw = str(Path(cfg.state_path).parent / "paper_status.json")
    return Path(raw)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def read_pid(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def clear_pid(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_status_file(path: Path, st: RunnerStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st.as_dict(), indent=2))


def load_status_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def get_status(cfg: Optional[Config] = None, run_id: Optional[str] = None) -> RunnerStatus:
    pf = pidfile_path(cfg, run_id=run_id)
    sf = status_path(cfg, run_id=run_id)
    prev = load_status_file(sf)
    pid = read_pid(pf)
    alive = pid_is_alive(pid) if pid is not None else False
    stale = bool(pid is not None and not alive)
    st = RunnerStatus(
        running=alive,
        pid=pid if (alive or stale) else None,
        pidfile=str(pf),
        stale=stale,
        started_at=prev.get("started_at"),
        last_iteration_at=prev.get("last_iteration_at"),
        last_result=prev.get("last_result"),
        last_error=prev.get("last_error"),
        last_equity=prev.get("last_equity"),
        poll_sec=prev.get("poll_sec"),
    )
    return st


def format_status(st: RunnerStatus) -> str:
    if st.running:
        state = f"running  pid={st.pid}"
    elif st.stale:
        state = f"not running (stale pidfile pid={st.pid})"
    else:
        state = "not running"
    lines = [
        f"paper runner: {state}",
        f"pidfile: {st.pidfile}",
        f"started_at: {st.started_at or '—'}",
        f"last_iteration_at: {st.last_iteration_at or '—'}",
        f"last_result: {st.last_result or '—'}",
        f"last_equity: {st.last_equity if st.last_equity is not None else '—'}",
        f"last_error: {st.last_error or '—'}",
        f"poll_sec: {st.poll_sec or '—'}",
        "Paper / dry-run only. Never live. Capital is allocated_capital, not OKX main balance.",
    ]
    return "\n".join(lines)


def start_background(
    cfg: Config,
    *,
    strategies: str = "d1",
    poll_sec: int = DEFAULT_POLL_SEC,
    config_path: Optional[str] = None,
    run_id: Optional[str] = None,
) -> tuple[int, str]:
    """Spawn `paper-loop` in the background. Returns (pid, message). Does not run if already alive."""
    st = get_status(cfg, run_id=run_id)
    if st.running:
        return st.pid or 0, f"already running pid={st.pid}"
    if st.stale:
        clear_pid(pidfile_path(cfg, run_id=run_id))
    cmd = [
        sys.executable,
        "-m",
        "tradingbot",
        *(["--config", config_path] if config_path else []),
        "paper-loop",
        "--strategies",
        strategies,
        "--poll",
        str(int(poll_sec)),
        *(["--run-id", run_id] if run_id else []),
    ]
    log_path = Path("logs") / "spec_v2" / run_id / "paper_runner.log" if run_id else Path("logs") / "paper_runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=os.getcwd(),
        env=env,
    )
    write_pid(pidfile_path(cfg, run_id=run_id), proc.pid)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_status_file(
        status_path(cfg, run_id=run_id),
        RunnerStatus(
            running=True,
            pid=proc.pid,
            pidfile=str(pidfile_path(cfg, run_id=run_id)),
            started_at=now,
            poll_sec=int(poll_sec),
            last_result="started",
        ),
    )
    return proc.pid, f"started pid={proc.pid} poll={poll_sec}s log={log_path}"


def stop_background(cfg: Optional[Config] = None, timeout: float = 8.0, run_id: Optional[str] = None) -> str:
    st = get_status(cfg, run_id=run_id)
    pf = pidfile_path(cfg, run_id=run_id)
    if st.stale or (st.pid and not st.running):
        clear_pid(pf)
        return f"cleared stale pidfile (pid={st.pid})"
    if not st.running or not st.pid:
        clear_pid(pf)
        return "not running"
    pid = st.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pid(pf)
        return f"process {pid} already gone"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_is_alive(pid):
            break
        time.sleep(0.1)
    if pid_is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    clear_pid(pf)
    prev = get_status(cfg, run_id=run_id)
    prev.running = False
    prev.pid = None
    prev.last_result = f"stopped pid={pid}"
    write_status_file(status_path(cfg, run_id=run_id), prev)
    return f"stopped pid={pid}"


def run_loop(cfg: Config, *, strategies: str = "d1", poll_sec: int = DEFAULT_POLL_SEC, run_id: Optional[str] = None) -> int:
    """Foreground poll loop. Called by `paper-loop`. Writes pid if missing (e.g. foreground)."""
    pf = pidfile_path(cfg, run_id=run_id)
    if read_pid(pf) is None:
        write_pid(pf, os.getpid())
    events_path = (
        str(Path("logs") / "spec_v2" / run_id / "paper_events.jsonl")
        if run_id
        else cfg.log_path
    )
    log = JsonlLogger(events_path)
    names = [s.strip() for s in strategies.split(",") if s.strip()][:3]
    if not names:
        names = ["d1"]
    log.info("paper_loop_start", strategies=names, poll_sec=poll_sec, pid=os.getpid())
    try:
        while True:
            for name in names:
                try:
                    from tradingbot.cli import run_paper_iteration

                    rc, summary = run_paper_iteration(cfg, strategy=name, update=True, bar=None, run_id=run_id)
                    eq = summary.get("equity")
                    write_status_file(
                        status_path(cfg, run_id=run_id),
                        RunnerStatus(
                            running=True,
                            pid=os.getpid(),
                            pidfile=str(pf),
                            started_at=load_status_file(status_path(cfg, run_id=run_id)).get("started_at"),
                            last_iteration_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            last_result=summary.get("message") or f"rc={rc}",
                            last_error=None,
                            last_equity=eq,
                            poll_sec=poll_sec,
                        ),
                    )
                    fields = {k: v for k, v in summary.items() if k not in ("book", "strategy", "rc")}
                    log.info("paper_loop_iter", strategy=name, rc=rc, **fields)
                except Exception as e:
                    log.error("paper_loop_iter_error", strategy=name, error=str(e))
                    st = get_status(cfg, run_id=run_id)
                    st.running = True
                    st.pid = os.getpid()
                    st.last_error = str(e)
                    st.last_iteration_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    write_status_file(status_path(cfg, run_id=run_id), st)
            time.sleep(max(5, int(poll_sec)))
    except (KeyboardInterrupt, SystemExit):
        log.info("paper_loop_stop", pid=os.getpid())
        return 0
    finally:
        if read_pid(pf) == os.getpid():
            clear_pid(pf)
