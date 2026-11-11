"""tradingbot CLI wrappers for kill/rearm/demo-dry-run. Paper only."""

from __future__ import annotations

from pathlib import Path

from tradingbot.cli import main


def test_demo_dry_run_without_demo_key_exits_zero(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in ("OKX_DEMO_API_KEY", "OKX_DEMO_API_SECRET", "OKX_DEMO_API_PASSPHRASE", "OKX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    repo = Path(__file__).resolve().parents[1]
    code = main(["--config", str(repo / "config.yaml"), "demo-dry-run"])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 0
    assert "demo key is needed" in text.lower()


def test_kill_and_rearm_use_worktree_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text((repo / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "state").mkdir()
    code = main(["--config", str(cfg_path), "kill", "--reason", "soak"])
    assert code == 0
    kill_file = tmp_path / "state" / "KILL_SWITCH"
    assert kill_file.exists()
    body = kill_file.read_text(encoding="utf-8")
    assert "tripped" in body
    code = main(["--config", str(cfg_path), "rearm"])
    assert code == 0
    text = kill_file.read_text(encoding="utf-8")
    assert "rearmed" in text
