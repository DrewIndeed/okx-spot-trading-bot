import os
import time

from tradingbot.config import Config
from tradingbot.paper_runner import (
    clear_pid,
    format_status,
    get_status,
    pid_is_alive,
    pidfile_path,
    read_pid,
    status_path,
    stop_background,
    write_pid,
    write_status_file,
    RunnerStatus,
)


def _cfg(tmp_path):
    # point state_path so pid/status live under tmp
    return Config(allocated_capital=10_000, state_path=str(tmp_path / "portfolio.json"))


def test_status_when_not_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    st = get_status(cfg)
    assert st.running is False
    assert st.stale is False
    text = format_status(st)
    assert "not running" in text
    assert "allocated_capital" in text


def test_alive_pid_and_stale_and_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    pf = pidfile_path(cfg)
    # stale
    write_pid(pf, 999_999_999)
    st = get_status(cfg)
    assert st.running is False
    assert st.stale is True
    assert "stale" in format_status(st)
    msg = stop_background(cfg)
    assert "stale" in msg
    assert read_pid(pf) is None

    # living process: sleep child
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        write_pid(pf, proc.pid)
        st = get_status(cfg)
        assert st.running is True
        assert st.pid == proc.pid
        assert pid_is_alive(proc.pid)
        msg = stop_background(cfg, timeout=3)
        assert "stopped" in msg
        proc.wait(timeout=3)  # reap so the pid is not a zombie
        assert proc.returncode is not None
        assert not pid_is_alive(proc.pid)
        assert read_pid(pf) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
