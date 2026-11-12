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

from tradingbot import paths as _paths
from tradingbot.config import Config
from tradingbot.logutil import JsonlLogger
from tradingbot.portfolio import Portfolio

DEFAULT_PIDFILE = "state/paper.pid"
DEFAULT_STATUS = "state/paper_status.json"
DEFAULT_POLL_SEC = 60
V3_ROOT = _paths.v3_root()
V3_OFFICIAL_RUN_ID = "spec_v3_official"
V2_OFFICIAL_RUN_ID = "spec_v2_official"


def is_v3_run(run_id: Optional[str] = None, strategies: Optional[str] = None) -> bool:
    if run_id and (run_id == V3_OFFICIAL_RUN_ID or str(run_id).startswith("spec_v3")):
        return True
    if strategies and str(strategies).strip().lower().startswith("v3"):
        return True
    # custom v3 run ids (e.g. "andrew_local"): known once started under <v3_root>/state/spec_v3/<id>
    if run_id and (V3_ROOT / "state" / "spec_v3" / str(run_id)).is_dir():
        return True
    return False


def _ensure_v3_state_dir(run_id: str) -> None:
    (V3_ROOT / "state" / "spec_v3" / str(run_id)).mkdir(parents=True, exist_ok=True)


def verify_v3_freeze(*args, **kwargs):
    from tradingbot.eval.v3_paper import verify_freeze

    return verify_freeze(*args, **kwargs)


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
    if is_v3_run(run_id):
        rid = run_id or V3_OFFICIAL_RUN_ID
        return V3_ROOT / "state" / "spec_v3" / rid / "paper.pid"
    if run_id:
        return Path("state") / "spec_v2" / run_id / "paper.pid"
    raw = DEFAULT_PIDFILE
    if cfg and getattr(cfg, "state_path", None):
        raw = str(Path(cfg.state_path).parent / "paper.pid")
    return Path(raw)


def status_path(cfg: Optional[Config] = None, run_id: Optional[str] = None) -> Path:
    if is_v3_run(run_id):
        rid = run_id or V3_OFFICIAL_RUN_ID
        return V3_ROOT / "state" / "spec_v3" / rid / "paper_status.json"
    if run_id:
        return Path("state") / "spec_v2" / run_id / "paper_status.json"
    raw = DEFAULT_STATUS
    if cfg and getattr(cfg, "state_path", None):
        raw = str(Path(cfg.state_path).parent / "paper_status.json")
    return Path(raw)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _win_pid_is_alive(pid: int) -> bool:
    """Windows: os.kill(pid, 0) is not a probe there (WinError 11 / terminates). Use OpenProcess."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate_pid(pid: int, *, force: bool = False) -> None:
    """POSIX: SIGTERM (SIGKILL if force), unchanged. Windows: TerminateProcess via ctypes.

    Raises ProcessLookupError when the process is already gone on both platforms.
    """
    if sys.platform == "win32":
        _win_terminate(pid)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def _win_terminate(pid: int) -> None:
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        raise ProcessLookupError(pid)
    try:
        if not kernel32.TerminateProcess(handle, 1):
            if not _win_pid_is_alive(pid):
                raise ProcessLookupError(pid)
            raise OSError(f"TerminateProcess failed for pid {pid}")
    finally:
        kernel32.CloseHandle(handle)


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
    if "spec_v3" in str(st.pidfile):
        lines.insert(-1, "status_md: /workspace/sprint/artifacts/spec_v3/paper_status.md")
        lines[-1] = "Paper / dry-run only. Never live. C_paper=10000, not OKX main balance. Not a strategy PASS."
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
    if run_id == V2_OFFICIAL_RUN_ID:
        return 0, "REFUSED: official v2 runner lives in /workspace/trading-bot (pid 129703); do not start it from the v3 worktree."
    if is_v3_run(run_id, strategies):
        run_id = run_id or V3_OFFICIAL_RUN_ID
        _ensure_v3_state_dir(run_id)  # so custom ids resolve to spec_v3 paths (pid/status)
        try:
            info = verify_v3_freeze()
        except Exception as e:
            # FreezeMismatch or anything else from verify — do not spawn.
            return 0, f"REFUSED: freeze mismatch: {e}"
    else:
        info = None
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
    if is_v3_run(run_id, strategies):
        log_path = V3_ROOT / "logs" / "spec_v3" / run_id / "paper_runner.log"
        cwd = str(V3_ROOT)
    else:
        log_path = Path("logs") / "spec_v2" / run_id / "paper_runner.log" if run_id else Path("logs") / "paper_runner.log"
        cwd = os.getcwd()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        # Windows: detach from the Git Bash console (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        **({"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32" else {}),
        cwd=cwd,
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
    msg = f"started pid={proc.pid} poll={poll_sec}s log={log_path}"
    if is_v3_run(run_id, strategies) and info is not None:
        from tradingbot.eval.v3_paper import format_hash_verification, ts_saigon, write_running_lock

        write_running_lock(pid=proc.pid, started=ts_saigon())
        msg = f"{msg}\n{format_hash_verification(info)}"
    return proc.pid, msg


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
        terminate_pid(pid)
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
            terminate_pid(pid, force=True)
        except ProcessLookupError:
            pass
    clear_pid(pf)
    if is_v3_run(run_id):
        from tradingbot.eval.v3_paper import clear_running_lock

        clear_running_lock(pid)
    prev = get_status(cfg, run_id=run_id)
    prev.running = False
    prev.pid = None
    prev.last_result = f"stopped pid={pid}"
    write_status_file(status_path(cfg, run_id=run_id), prev)
    return f"stopped pid={pid}"


def run_loop(cfg: Config, *, strategies: str = "d1", poll_sec: int = DEFAULT_POLL_SEC, run_id: Optional[str] = None) -> int:
    """Foreground poll loop. Called by `paper-loop`. Writes pid if missing (e.g. foreground)."""
    if run_id == V2_OFFICIAL_RUN_ID:
        raise SystemExit("REFUSED: official v2 runner lives in /workspace/trading-bot; do not start it from the v3 worktree.")
    if is_v3_run(run_id, strategies):
        run_id = run_id or V3_OFFICIAL_RUN_ID
        _ensure_v3_state_dir(run_id)
        verify_v3_freeze()
    pf = pidfile_path(cfg, run_id=run_id)
    if read_pid(pf) is None:
        write_pid(pf, os.getpid())
    if is_v3_run(run_id, strategies):
        from tradingbot.eval.v3_paper import ts_saigon, write_running_lock

        write_running_lock(pid=os.getpid(), started=ts_saigon())
        events_path = str(V3_ROOT / "logs" / "spec_v3" / run_id / "paper_events.jsonl")
    else:
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
        if is_v3_run(run_id, strategies):
            from tradingbot.eval.v3_paper import clear_running_lock

            clear_running_lock(os.getpid())
