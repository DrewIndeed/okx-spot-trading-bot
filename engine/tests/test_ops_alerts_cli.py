from __future__ import annotations

from pathlib import Path

from okxspot.alerts import FileAlerter, MemoryAlerter, build_alerter
from okxspot.cli import main
from okxspot.killswitch import KillSwitch
from tests.conftest import make_settings, write_config


def test_file_alerter_writes_required_events(tmp_path: Path) -> None:
    path = tmp_path / "alerts.jsonl"
    alerter = FileAlerter(path)
    for event, msg in [
        ("kill_switch", "tripped"),
        ("restart", "child exited"),
        ("stale_data", "stale_candle"),
        ("recon_mismatch", "missing cid"),
        ("risk_block", "daily_loss_limit"),
        ("daily_summary", "equity=1000 errors=0"),
    ]:
        alerter.send("warning", msg, event=event)
    text = path.read_text(encoding="utf-8")
    for event in ("kill_switch", "restart", "stale_data", "recon_mismatch", "risk_block", "daily_summary"):
        assert event in text
    assert "TELEGRAM" not in text
    mem = MemoryAlerter()
    mem.send("info", "hello", event="restart")
    assert mem.events[0].event == "restart"


def test_build_alerter_defaults_to_log_and_optional_file(tmp_path: Path) -> None:
    settings = make_settings(telegram={"enabled": False}, alerts={"file_path": str(tmp_path / "a.jsonl")})
    alerter = build_alerter(settings)
    alerter.send("info", "ping", event="daily_summary")
    assert (tmp_path / "a.jsonl").exists()


def test_kill_and_rearm_cli(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = write_config(tmp_path, kill_switch_path=str(tmp_path / "KILL_SWITCH"))
    assert main(["--log-level", "ERROR", "kill", "--config", str(cfg), "--reason", "operator"]) == 0
    out = capsys.readouterr().out
    assert "tripped" in out.lower() or "kill" in out.lower()
    ks = KillSwitch(tmp_path / "KILL_SWITCH")
    assert ks.is_tripped is True
    assert main(["--log-level", "ERROR", "rearm", "--config", str(cfg)]) == 0
    assert KillSwitch(tmp_path / "KILL_SWITCH").is_tripped is False
