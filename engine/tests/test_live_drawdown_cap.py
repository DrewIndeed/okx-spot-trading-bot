"""Live configs must refuse max_drawdown_pct above 0.15."""
import pytest

from okxspot.errors import LiveGuardError
from okxspot.runtime import prepare_live
from tests.conftest import make_settings


def test_paper_may_use_wider_kill_switch():
    assert str(make_settings(mode="paper", risk={"max_drawdown_pct": "0.30"}).risk.max_drawdown_pct) == "0.30"


def test_live_accepts_cap_at_or_below_15pct():
    assert make_settings(mode="live", risk={"max_drawdown_pct": "0.15"}).mode == "live"


@pytest.mark.parametrize("overrides", [{"mode": "live"}, {"live_enabled": True}])
def test_live_config_refuses_drawdown_above_15pct(overrides):
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        make_settings(risk={"max_drawdown_pct": "0.16"}, **overrides)


def test_prepare_live_rechecks_cap_before_any_key_read():
    s = make_settings(mode="live", risk={"max_drawdown_pct": "0.15"})
    s.risk.max_drawdown_pct = s.risk.max_drawdown_pct * 2  # mutated after validation
    with pytest.raises(LiveGuardError, match="max_drawdown_pct"):
        prepare_live(s, enable_live_flag=True, env={})
