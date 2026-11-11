import os
from pathlib import Path

import pytest

from tradingbot.config import Config, LiveTradingRefused, ensure_safe_mode, load_config
from tradingbot.orders import OrderManager


def test_dry_run_ok():
    cfg = Config(allocated_capital=10_000, mode="dry_run")
    ensure_safe_mode(cfg, live_flag=False)


def test_live_mode_without_gates_refused():
    cfg = Config(allocated_capital=10_000, mode="live")
    with pytest.raises(LiveTradingRefused):
        ensure_safe_mode(cfg, live_flag=False)


def test_live_flag_without_env_refused(monkeypatch):
    monkeypatch.delenv("LIVE_APPROVED", raising=False)
    cfg = Config(allocated_capital=10_000, mode="dry_run")
    with pytest.raises(LiveTradingRefused):
        ensure_safe_mode(cfg, live_flag=True)


def test_all_gates_still_not_implemented(monkeypatch):
    monkeypatch.setenv("LIVE_APPROVED", "1")
    cfg = Config(allocated_capital=10_000, mode="live")
    with pytest.raises(NotImplementedError, match="not implemented"):
        ensure_safe_mode(cfg, live_flag=True)


def test_order_manager_live_refused():
    om = OrderManager()
    with pytest.raises(LiveTradingRefused):
        om.submit_live(None, None)
