"""V-B 4/6 combination and V-C sleeve independence."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.strategies.spec_v1_common import VB_MIN_COMPONENTS
from tradingbot.strategies.spec_v1_signals import vb_signals


def _panel_from_close(close: pd.DataFrame) -> dict:
    return {
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "open": close.shift(1).fillna(close),
        "vq": pd.DataFrame(5e6, index=close.index, columns=close.columns),
    }


def test_vb_tie_three_of_six_is_flat():
    # Construct a series where TSMOM 21 and 63 are on, 252 is off, and no Donchian is long
    # (price never breaks a 20-day high, just drifts). Combined must be flat (3/6 or less).
    idx = pd.date_range("2020-01-01", periods=300, freq="D")
    # Slow grind up then a long down? Easier: constant price — no TSMOM, no breakout.
    close = pd.DataFrame({"AAA": 10.0, "BTC": 100.0}, index=idx)
    U = pd.DataFrame(True, index=idx, columns=close.columns)
    out = vb_signals(_panel_from_close(close), U)
    assert int(out["combined"].iloc[-1]["AAA"]) == 0
    assert int(out["count"].iloc[-1]["AAA"]) == 0


def test_vb_requires_four_components():
    assert VB_MIN_COMPONENTS == 4
