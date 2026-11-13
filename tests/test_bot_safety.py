"""Bot safety: drawdown watch, live refusal, bot scope guard, alerts/Telegram redaction, side views."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from tests._botkit import SYMS, StubFeed, make_cfg, series
from tradingbot import alerts, cli, paper_runner, tgbot
from tradingbot.config import LiveTradingRefused, ensure_safe_mode
from tradingbot.data.store import save_candles
from tradingbot.dd_watch import check_breaches

TOKEN = "TESTONLY-not-a-real-telegram-token-000000"


# ---- drawdown watch -------------------------------------------------------

def test_drawdown_levels_logged_once_per_crossing(tmp_path):
    sp = tmp_path / "state" / "portfolio.json"
    seen = []
    for eq in (9_000, 8_400, 8_300, 7_900, 7_800, 8_800, 8_400):
        _dd, ev = check_breaches(eq, 10_000, day="d", state_path=sp)
        seen.append([e["level"] for e in ev])
    assert seen == [[], [-0.15], [], [-0.20], [], [], [-0.15]]
    data = json.loads((tmp_path / "state" / "dd_breaches.json").read_text())
    assert len(data["events"]) == 3


def test_paper_once_my_d1_reports_breach_without_kill(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    from tradingbot.portfolio import Portfolio
    book = Portfolio.new(10_000.0)
    book.equity_peak = 13_000.0  # current 10k = -23% from peak
    book.save(cfg.state_path)
    monkeypatch.setattr(cli, "_feed", lambda _cfg: StubFeed())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    rc, s = cli.run_paper_iteration(cfg, strategy="my_d1", update=False)
    assert rc == 0
    assert [e["level"] for e in s["drawdown_breaches"]] == [-0.15, -0.20]
    assert "drawdown breach" in s["message"] and s["halted"] is False
    rc, s = cli.run_paper_iteration(cfg, strategy="my_d1", update=False)
    assert rc == 0 and s["drawdown_breaches"] == []
    events = [json.loads(l)["event"] for l in open(cfg.log_path)]
    assert events.count("drawdown_breach") == 2


def test_paper_once_my_d1_smoke_with_stub_feed(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    feed = StubFeed()
    monkeypatch.setattr(cli, "_feed", lambda _cfg: feed)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    rc, s = cli.run_paper_iteration(cfg, strategy="my_d1", update=False)
    assert rc == 0 and s["strategy"] == "my_d1" and s["ready"]
    assert {i for i, _ in feed.calls} == {f"{b}-USDT" for b in SYMS}
    assert (tmp_path / "state" / "portfolio.json").exists()
    assert (tmp_path / "logs" / "paper_book.jsonl").exists()


# ---- live stays refused ---------------------------------------------------

def test_live_refused_and_paper_drawdown_never_reaches_live(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    ensure_safe_mode(cfg)  # paper ok
    with pytest.raises(LiveTradingRefused, match="max_drawdown <= 0.15"):
        ensure_safe_mode(cfg, live_flag=True)
    monkeypatch.setenv("LIVE_APPROVED", "1")
    cfg.mode = "live"
    with pytest.raises(LiveTradingRefused, match="max_drawdown"):
        ensure_safe_mode(cfg, live_flag=True)
    cfg.risk.max_drawdown = 0.15
    with pytest.raises(NotImplementedError):  # still refused: live is not implemented
        ensure_safe_mode(cfg, live_flag=True)
    monkeypatch.delenv("LIVE_APPROVED")
    assert cli.main(["--live", "paper-status"]) == 3


# ---- bot scope guard ------------------------------------------------------

def _cfg_here(tmp_path, monkeypatch):
    import shutil
    from tests._botkit import REPO
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADINGBOT_CONFIG", raising=False)


def test_scope_guard_refuses_official_runs_and_locked_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paper_runner.assert_bot_scope().name == "paper.pid"
    with pytest.raises(paper_runner.ProtectedRunError):
        paper_runner.assert_bot_scope(run_id="spec_v2_official")
    with pytest.raises(paper_runner.ProtectedRunError):
        paper_runner.assert_bot_scope(strategies="v2")
    (tmp_path / "RUNNING.lock").write_text("official")
    with pytest.raises(paper_runner.ProtectedRunError, match="RUNNING.lock"):
        paper_runner.assert_bot_scope()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc cmdline")
def test_scope_guard_refuses_pidfile_pointing_at_official_run(tmp_path, monkeypatch):
    _cfg_here(tmp_path, monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "spec_v2_official"])
    try:
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "paper.pid").write_text(str(proc.pid))
        with pytest.raises(paper_runner.ProtectedRunError, match="official"):
            paper_runner.assert_bot_scope()
        assert cli.main(["paper-stop", "--scope", "my_d1"]) == 3
        assert proc.poll() is None  # not signalled
    finally:
        proc.kill()
        proc.wait()


def test_cli_scope_refuses_in_locked_dir(tmp_path, monkeypatch, capsys):
    _cfg_here(tmp_path, monkeypatch)
    (tmp_path / "RUNNING.lock").write_text("x")
    for argv in (["paper-status", "--scope", "my_d1"], ["paper-stop", "--scope", "my_d1"],
                 ["paper-start", "--strategies", "my_d1", "--scope", "my_d1"]):
        assert cli.main(argv) == 3
    assert "REFUSED" in capsys.readouterr().out


# ---- Telegram + alerts ----------------------------------------------------

def test_telegram_commands_are_scoped_and_never_live():
    for name, (args, _l, _d) in tgbot.COMMANDS.items():
        assert "--live" not in args and "--run-id" not in args, name
        if args and args[0] in ("paper-start", "paper-stop", "paper-status"):
            assert args[-2:] == ["--scope", "my_d1"], name
    assert "live" not in tgbot.COMMANDS


def test_telegram_unknown_live_command_gets_menu(monkeypatch):
    sent = []
    monkeypatch.setattr(tgbot, "_send", lambda t, c, text: sent.append(text))
    monkeypatch.setattr(tgbot, "_safe_cli", lambda *a, **k: pytest.fail("cli must not run"))
    tgbot._handle(TOKEN, "1", "/live --live", threading.Lock())
    assert sent and sent[0].startswith("Unknown command /live")


def test_telegram_cli_error_is_redacted(monkeypatch):
    sent = []
    monkeypatch.setattr(tgbot, "_send", lambda t, c, text: sent.append(text))
    monkeypatch.setattr(tgbot, "_cli", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"boom https://api.telegram.org/bot{TOKEN}/x")))
    tgbot._handle(TOKEN, "1", "/status", threading.Lock())
    assert sent and TOKEN not in sent[0] and "***" in sent[0]


def test_tg_bot_loop_allow_list_and_redacted_errors(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    handled = []
    monkeypatch.setattr(tgbot, "_handle", lambda t, chat, text, lock: handled.append((chat, text)))
    monkeypatch.setattr(tgbot, "_send", lambda *a: None)
    calls = {"n": 0}

    def api(token, method, payload=None, timeout=35):
        if method == "getMe":
            return {"ok": True, "result": {"username": "testbot"}}
        if method == "setMyCommands":
            raise RuntimeError(f"bad url https://api.telegram.org/bot{token}/setMyCommands")
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": [
                {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/stoploop"}},
                {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/status"}},
            ]}
        raise KeyboardInterrupt

    monkeypatch.setattr(tgbot, "_api", api)
    assert tgbot.run_tg_bot() == 0
    assert handled == [("42", "/status")]  # chat 7 silently ignored
    out = capsys.readouterr().out
    assert TOKEN not in out and "***" in out


def test_tg_bot_getme_exception_redacted(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(tgbot, "_api", lambda token, *a, **k: (_ for _ in ()).throw(OSError(f"x bot{token} y")))
    assert tgbot.run_tg_bot() == 2
    assert TOKEN not in capsys.readouterr().out


def test_send_chunks_long_text(monkeypatch):
    payloads = []
    monkeypatch.setattr(tgbot, "_api", lambda token, method, payload=None, timeout=35: payloads.append(payload))
    tgbot._send(TOKEN, "42", "x" * 8000)
    assert len(payloads) == 3 and all(len(p["text"]) <= 3600 for p in payloads)


def test_alert_noop_without_token(tmp_path, monkeypatch):
    from tradingbot.logutil import JsonlLogger
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(alerts, "_post", lambda *a: pytest.fail("must not post"))
    log = JsonlLogger(str(tmp_path / "a.jsonl"))
    assert alerts.send_alert("hi", logger=log) is False


def test_alert_failure_never_logs_token(tmp_path, monkeypatch, capsys):
    import urllib.request
    from tradingbot.logutil import JsonlLogger
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(req, timeout=10):
        raise OSError(f"failed {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    path = tmp_path / "a.jsonl"
    assert alerts.send_alert("hello", logger=JsonlLogger(str(path))) is False
    assert TOKEN not in path.read_text() and TOKEN not in capsys.readouterr().out


# ---- side views -----------------------------------------------------------

def test_side_signals_prints_paper_levels(tmp_path, monkeypatch, capsys):
    from tradingbot import side_signals
    cfg = make_cfg(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(side_signals, "_feed", lambda _cfg: StubFeed())
    rc = side_signals.print_signals(cfg, update=False, days=400)
    out = capsys.readouterr().out
    assert rc == 0 and "Not a live order" in out
    assert (tmp_path / "logs" / "signals_latest.md").exists()


def test_summary_view_reads_closed_cache_only(tmp_path, monkeypatch, capsys):
    pytest.importorskip("pyarrow")
    from tradingbot import summary_view
    from tradingbot.data.rest import OkxRestFeed
    from tradingbot.types import Candle
    cfg = make_cfg(tmp_path)
    for b in SYMS:
        rows = series(b)
        last = rows[-1]
        rows.append(Candle(ts=last.ts + 86_400_000, open=1, high=1, low=1, close=1e9,
                           volume_base=1, volume_quote=5e6, confirm=0))
        save_candles(cfg.data.cache_dir, f"{b}-USDT", "1Dutc", rows, backend="parquet")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(OkxRestFeed, "get_spot_tickers", lambda self: {})
    rc = summary_view.print_summary(cfg, top=3)
    out = capsys.readouterr().out
    assert rc == 0 and "1000000000" not in out.replace(",", "")
