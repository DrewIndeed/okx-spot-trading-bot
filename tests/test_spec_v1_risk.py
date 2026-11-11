"""Risk overlays: 10% name cap, 100% gross, 1% stop risk, kill-switch threshold."""

from __future__ import annotations

import pandas as pd

from tradingbot.eval.engine import _clip_name_and_gross
from tradingbot.strategies.spec_v1_common import GROSS_CAP, KILL_SWITCH_DD, NAME_CAP, RISK_PCT


def test_name_and_gross_caps():
    w = pd.Series({"A": 0.4, "B": 0.4, "C": 0.4})
    out = _clip_name_and_gross(w)
    assert float(out.max()) <= NAME_CAP + 1e-12
    assert float(out.sum()) <= GROSS_CAP + 1e-12


def test_frozen_risk_constants():
    assert NAME_CAP == 0.10
    assert GROSS_CAP == 1.0
    assert RISK_PCT == 0.01
    assert KILL_SWITCH_DD == -0.15
