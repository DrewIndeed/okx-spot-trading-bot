from __future__ import annotations

from pathlib import Path

from okxspot.cli import main
from okxspot.safety import LIVE_CONFIRM_VALUE
from tests.conftest import write_config


def test_paper_demo_and_backtest_demo(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = write_config(tmp_path)
    code = main(
        ["--log-level", "ERROR", "paper", "--config", str(cfg), "--demo", "--once"],
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "not a performance claim" in output
    assert "profitable" in output

    out = tmp_path / "report"
    code = main(
        ["--log-level", "ERROR", "backtest", "--config", str(cfg), "--demo", "--output", str(out)],
    )
    assert code == 0
    assert (out / "equity.png").read_bytes().startswith(b"\x89PNG")
    assert (out / "backtest.json").exists()


def test_backtest_without_range_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = write_config(tmp_path)
    code = main(["--log-level", "ERROR", "backtest", "--config", str(cfg)])
    assert code == 1


def test_live_never_connects_without_gates(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called: list[str] = []
    monkeypatch.setattr("okxspot.runtime.connect_okx", lambda *a, **k: called.append("connect"))
    cfg = write_config(tmp_path)
    assert main(["--log-level", "ERROR", "live", "--config", str(cfg)], env={}) == 2
    env = {"OKX_LIVE_CONFIRM": "nope"}
    assert (
        main(["--log-level", "ERROR", "live", "--config", str(cfg), "--enable-live"], env=env) == 2
    )
    big = write_config(tmp_path, allocated_capital="5000", max_allocated_capital="1000")
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
    }
    assert main(["--log-level", "ERROR", "live", "--config", str(big), "--enable-live"], env=env) == 2
    assert called == []


def test_live_refuses_withdraw_permission(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from okxspot.exchange.okx_parse import AccountConfig

    monkeypatch.setattr("okxspot.runtime.connect_okx", lambda *a, **k: object())
    monkeypatch.setattr(
        "okxspot.runtime.fetch_account_config",
        lambda client, settings: AccountConfig(
            permissions={"trade", "withdraw"},
            ip="1.2.3.4",
            uid="sub",
            main_uid="main",
            label="bot",
        ),
    )
    ran: list[str] = []
    monkeypatch.setattr("okxspot.cli.run_live_loop", lambda *a, **k: ran.append("ran"))
    cfg = write_config(tmp_path, live_enabled=True)
    env = {
        "OKX_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "OKX_API_KEY": "test-key",
        "OKX_API_SECRET": "test-secret",
        "OKX_API_PASSPHRASE": "test-pass",
    }
    code = main(["--log-level", "ERROR", "live", "--config", str(cfg), "--enable-live"], env=env)
    assert code == 2
    assert ran == []


def test_env_example_has_no_real_secrets() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "OKX_API_KEY=" in text
    assert "OKX_LIVE_CONFIRM=" in text
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            _, value = line.split("=", 1)
            assert value.strip() == ""
