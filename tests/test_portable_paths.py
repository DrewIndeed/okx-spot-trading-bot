"""v3-local portability: path resolution, frozen-hash parity, Windows pid probe."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tradingbot import paths
from tradingbot import paper_runner as pr

REPO = paths.REPO_ROOT


def test_vendored_inputs_exist_and_brief_hash_matches_freeze():
    freeze = json.loads((REPO / "config/v3/freeze.json").read_text(encoding="utf-8"))
    brief = (REPO / "config/v3/strategy-v3-brief.md").read_bytes()
    assert hashlib.sha256(brief).hexdigest() == freeze["sha256_brief"]
    assert freeze["sha256_brief"].startswith("6544095c")
    assert freeze["sha256_v3_code"] == "533ee52ed5945f0324bdfe1dd37f172684ba732f4da5fd31b2260e15c2d691bb"
    assert (REPO / "config/v3/favorites.txt").read_text(encoding="utf-8").strip()


def test_defaults_prefer_repo_copy(monkeypatch):
    for k in ("TB_FAVORITES", "TB_V3_FREEZE", "TB_V3_BRIEF"):
        monkeypatch.delenv(k, raising=False)
    assert paths.v3_freeze_path() == REPO / "config/v3/freeze.json"
    assert paths.v3_brief_path() == REPO / "config/v3/strategy-v3-brief.md"
    assert paths.favorites_path() == REPO / "config/v3/favorites.txt"
    # explicit config path that does not exist (e.g. /workspace on Windows) falls back to repo copy
    assert paths.favorites_path("/no/such/dir/favorites.txt") == REPO / "config/v3/favorites.txt"


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("TB_FAVORITES", str(tmp_path / "f.txt"))
    monkeypatch.setenv("TB_V3_FREEZE", str(tmp_path / "z.json"))
    monkeypatch.setenv("TB_V3_BRIEF", str(tmp_path / "b.md"))
    monkeypatch.setenv("TB_V3_ROOT", str(tmp_path))
    assert paths.favorites_path("/x") == tmp_path / "f.txt"
    assert paths.v3_freeze_path() == tmp_path / "z.json"
    assert paths.v3_brief_path() == tmp_path / "b.md"
    assert paths.v3_root() == tmp_path
    assert paths.v3_artifact_root() == tmp_path / "state" / "artifacts" / "spec_v3"


def test_frozen_code_hash_matches_freeze_and_frozen_definition():
    expected = json.loads((REPO / "config/v3/freeze.json").read_text(encoding="utf-8"))["sha256_v3_code"]
    assert paths.frozen_code_hash() == expected
    # identical to the frozen definition when the code sits at the official box path
    official = paths.OFFICIAL_V3_ROOT
    if (official / "tradingbot/eval/v3_protocol.py").exists():
        from tradingbot.eval.v3_protocol import sha256_paths

        assert sha256_paths() == paths.frozen_code_hash(official)


def test_frozen_code_hash_detects_edit(tmp_path):
    for p in (REPO / "tradingbot/eval").glob("v3_*.py"):
        d = tmp_path / "tradingbot/eval"
        d.mkdir(parents=True, exist_ok=True)
        (d / p.name).write_bytes(p.read_bytes())
    good = paths.frozen_code_hash(tmp_path)
    f = tmp_path / "tradingbot/eval/v3_engine.py"
    f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # Windows autocrlf checkout: same digest
    assert paths.frozen_code_hash(tmp_path) == good
    f.write_bytes(f.read_bytes() + b"# edit\n")  # any real edit is refused
    assert paths.frozen_code_hash(tmp_path) != good


def test_verify_freeze_uses_repo_freeze():
    from tradingbot.eval.v3_paper import verify_freeze

    info = verify_freeze(REPO / "config/v3/freeze.json")
    assert info["ok"] and info["sha256_v3_code"].startswith("533ee52e")


def test_pid_is_alive_posix_unchanged(monkeypatch):
    import os

    monkeypatch.setattr(pr.sys, "platform", "linux")
    if sys.platform == "win32":
        pytest.skip("POSIX probe only")
    assert pr.pid_is_alive(os.getpid()) is True
    assert pr.pid_is_alive(0) is False

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(pr.os, "kill", gone)
    assert pr.pid_is_alive(12345) is False


def test_pid_is_alive_windows_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(pr.sys, "platform", "win32")
    monkeypatch.setattr(pr, "_win_pid_is_alive", lambda pid: calls.append(pid) or pid == 42)

    def boom(*a, **k):  # os.kill(pid, 0) must never be used on Windows
        raise OSError(11, "WinError 11")

    monkeypatch.setattr(pr.os, "kill", boom)
    assert pr.pid_is_alive(42) is True
    assert pr.pid_is_alive(7) is False
    assert calls == [42, 7]


def test_win_probe_with_fake_kernel32(monkeypatch):
    import ctypes

    class K:
        def __init__(self, handle, code, err=0):
            self.h, self.c, self.e = handle, code, err

        def OpenProcess(self, *a):
            return self.h

        def GetLastError(self):
            return self.e

        def GetExitCodeProcess(self, h, ref):
            ref._obj.value = self.c
            return 1

        def CloseHandle(self, h):
            return 1

    class W:
        kernel32 = None

    w = W()
    monkeypatch.setattr(ctypes, "windll", w, raising=False)
    w.kernel32 = K(1, 259)
    assert pr._win_pid_is_alive(5) is True
    w.kernel32 = K(1, 0)
    assert pr._win_pid_is_alive(5) is False
    w.kernel32 = K(0, 0, err=87)  # ERROR_INVALID_PARAMETER: no such pid
    assert pr._win_pid_is_alive(5) is False
    w.kernel32 = K(0, 0, err=5)  # access denied: exists
    assert pr._win_pid_is_alive(5) is True


def test_terminate_posix_unchanged(monkeypatch):
    import signal

    sent = []
    monkeypatch.setattr(pr.sys, "platform", "linux")
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    pr.terminate_pid(9)
    if hasattr(signal, "SIGKILL"):
        pr.terminate_pid(9, force=True)
        assert sent == [(9, signal.SIGTERM), (9, signal.SIGKILL)]


def test_terminate_windows_uses_terminateprocess(monkeypatch):
    import ctypes

    calls = []

    class K:
        def __init__(self, handle):
            self.h = handle

        def OpenProcess(self, access, inherit, pid):
            calls.append(("open", access, pid))
            return self.h

        def TerminateProcess(self, h, code):
            calls.append(("term", h))
            return 1

        def CloseHandle(self, h):
            calls.append(("close", h))
            return 1

    class W:
        kernel32 = K(77)

    monkeypatch.setattr(pr.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", W(), raising=False)
    monkeypatch.setattr(pr.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("os.kill used on Windows")))
    pr.terminate_pid(5, force=True)
    assert calls == [("open", 1, 5), ("term", 77), ("close", 77)]
    W.kernel32 = K(0)
    with pytest.raises(ProcessLookupError):
        pr.terminate_pid(5)


def test_custom_v3_run_id_is_found_by_status(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "V3_ROOT", tmp_path)
    assert pr.is_v3_run("andrew_local") is False
    (tmp_path / "state" / "spec_v3" / "andrew_local").mkdir(parents=True)
    assert pr.is_v3_run("andrew_local") is True
    assert pr.pidfile_path(run_id="andrew_local") == tmp_path / "state/spec_v3/andrew_local/paper.pid"


def test_start_background_custom_id_writes_v3_pidfile(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "V3_ROOT", tmp_path)
    monkeypatch.setattr(pr, "verify_v3_freeze", lambda: None)

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(pr.subprocess, "Popen", lambda *a, **k: FakeProc())
    pid, _ = pr.start_background(None, strategies="v3", run_id="andrew_local")
    assert pid == 4242
    assert (tmp_path / "state/spec_v3/andrew_local/paper.pid").read_text() == "4242"
    assert not (tmp_path / "state/spec_v2").exists()


def test_favorites_relative_config_resolves_to_repo(monkeypatch):
    monkeypatch.delenv("TB_FAVORITES", raising=False)
    from tradingbot.config import load_config
    from tradingbot.universe import load_favorites

    cfg = load_config(str(REPO / "config.yaml"))
    assert paths.favorites_path(cfg.universe_file) == REPO / "config/v3/favorites.txt"
    assert load_favorites(cfg.universe_file)[0] == "BTC"
