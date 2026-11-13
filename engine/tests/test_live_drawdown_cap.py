"""A paper kill switch above 15% must never carry over to a live engine config."""
import pytest

from tests.conftest import make_settings


def test_paper_may_use_wide_paper_kill_switch():
    assert str(make_settings(mode="paper", risk={"max_drawdown_pct": "0.30"}).risk.max_drawdown_pct) == "0.30"


def test_live_accepts_cap_at_or_below_15pct():
    assert make_settings(mode="live", risk={"max_drawdown_pct": "0.15"}).mode == "live"


def test_live_refuses_paper_drawdown_value():
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        make_settings(mode="live", risk={"max_drawdown_pct": "0.30"})
