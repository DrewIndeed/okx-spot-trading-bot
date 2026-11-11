"""allocated_capital / paper_notional is required and is the only sizing base."""

from pathlib import Path

import pytest

from tradingbot.config import AllocatedCapitalRequired, Config, load_config
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskManager
from tradingbot.types import Signal


def test_config_missing_allocated_capital_raises(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("mode: dry_run\nsymbol: BTC-USDT\n")
    with pytest.raises(AllocatedCapitalRequired):
        load_config(str(p))


def test_config_construct_without_capital_raises():
    with pytest.raises(AllocatedCapitalRequired):
        Config()


def test_paper_notional_alias_accepted(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("mode: dry_run\npaper_notional_usdt: 2500\n")
    cfg = load_config(str(p))
    assert cfg.allocated_capital == 2500
    assert cfg.paper_notional_usdt == 2500


def test_mismatch_capital_keys_raise(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("allocated_capital: 1000\npaper_notional_usdt: 2000\n")
    with pytest.raises(AllocatedCapitalRequired, match="must be equal"):
        load_config(str(p))


def test_sizing_uses_allocated_capital():
    """Qty scales with allocated_capital; there is no account-balance input."""
    def qty_for(capital: float) -> float:
        cfg = Config(allocated_capital=capital)
        book = Portfolio.new(capital)
        rm = RiskManager(cfg.risk, cfg.allocated_capital)
        # pin risk to 1% for an exact figure
        rm.risk_pct = 0.01
        sig = Signal(
            inst_id="BTC-USDT",
            side="buy",
            ts=1,
            signal_id=f"t:{capital}:buy",
            stop_price=90.0,
        )
        d = rm.evaluate(sig, book, price=100.0)
        assert d.allowed, d.reason
        return d.qty

    q10k = qty_for(10_000)
    q5k = qty_for(5_000)
    # 1% of 10_000 / (100-90) = 10; of 5_000 = 5
    assert q10k == pytest.approx(10.0)
    assert q5k == pytest.approx(5.0)
    assert q10k == pytest.approx(2 * q5k)
    # RiskManager has no account_balance parameter
    assert "account_balance" not in RiskManager.evaluate.__code__.co_varnames
