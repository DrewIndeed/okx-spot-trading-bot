"""tradingbot live path refuses a paper-sized max_drawdown."""
import pytest

from tradingbot.config import Config, LiveTradingRefused, ensure_safe_mode


def _cfg(dd):
    cfg = Config(allocated_capital=1000.0)
    cfg.risk.max_drawdown = dd
    return cfg


def test_paper_unaffected_by_live_cap():
    ensure_safe_mode(_cfg(0.30))


def test_live_refuses_drawdown_above_15pct(monkeypatch):
    monkeypatch.setenv("LIVE_APPROVED", "1")
    cfg = _cfg(0.30)
    cfg.mode = "live"
    with pytest.raises(LiveTradingRefused, match="max_drawdown <= 0.15"):
        ensure_safe_mode(cfg, live_flag=True)
    cfg.risk.max_drawdown = 0.15
    with pytest.raises(NotImplementedError):  # cap passes; live itself still refused
        ensure_safe_mode(cfg, live_flag=True)
