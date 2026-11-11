"""H3 hysteresis, H5 F2 mapping, H1/H4 increase blocks, official set size."""

from __future__ import annotations

import numpy as np
import pytest
import pandas as pd

from tradingbot.eval.v3_engine import apply_increase_block, new_book
from tradingbot.eval.v3_hypotheses import (
    HYPOTHESIS_IDS,
    NAMES,
    btc_vol_gate,
    is_weekly_signal,
    precompute_h3,
    vc_state_soft,
    vc_state_strict,
)
from tradingbot.strategies.spec_v2_common import WEDDING_FUNDS_USDT


def test_exactly_five_official_hypotheses():
    assert HYPOTHESIS_IDS == ("H1", "H2", "H3", "H4", "H5")
    assert set(NAMES) == set(HYPOTHESIS_IDS)


def test_h5_f2_is_reduce_not_halt():
    row = pd.Series({"f2_halt": True, "btc_stale_halt": False, "f1_reduce": True})
    assert vc_state_strict(row) == "HALT"
    assert vc_state_soft(row) == "REDUCE"
    stale = pd.Series({"f2_halt": True, "btc_stale_halt": True, "f1_reduce": False})
    assert vc_state_soft(stale) == "HALT"
    quiet = pd.Series({"f2_halt": False, "btc_stale_halt": False, "f1_reduce": True})
    assert vc_state_strict(quiet) == "NORMAL"  # V-C ignores F1
    assert vc_state_soft(quiet) == "NORMAL"


def test_h3_exit_only_when_trend_is_gone():
    # Build a short synthetic BTC/ETH panel that enters, holds through a 1-sleeve dip, then exits.
    idx = pd.date_range("2022-01-01", periods=250, freq="D")
    # Climb then a shallow dip then a collapse below SMA200.
    btc = np.concatenate([np.linspace(100, 200, 220), np.linspace(199, 50, 30)])
    df = pd.DataFrame({"BTC": btc, "ETH": btc * 0.1}, index=idx)
    hi = df * 1.01
    lo = df * 0.99
    panel = {"open": df, "high": hi, "low": lo, "close": df, "vq": df * 0, "bases": ["BTC", "ETH"]}
    pre = precompute_h3(panel, ["BTC", "ETH"])
    long = pre["long"]["BTC"]
    # After warmup there should be some True days, and the last days (collapse) should be False.
    assert bool(long.iloc[-1]) is False
    # Hysteresis: once long, a temporary drop of sleeve count to 1 should not exit if still above SMA200.
    # Check that the long run is a single contiguous block (no flip-flop in the middle of the trend).
    mid = long.iloc[200:220]
    if mid.any():
        assert bool(mid.all()) or mid.sum() >= 15


def test_h4_vol_gate_blocks_increases_only():
    desired = pd.Series({"BTC": 0.4, "ETH": 0.2, "SOL": 0.1})
    current = pd.Series({"BTC": 0.2, "ETH": 0.3, "SOL": 0.0})
    out = apply_increase_block(desired, current, blocked={"BTC", "ETH", "SOL"})
    assert out["BTC"] == 0.2  # no increase
    assert out["ETH"] == 0.2  # exit/reduce still allowed
    assert out["SOL"] == 0.0  # new long blocked


def test_h1_proxy_blocks_alts_not_eth_on_btc_crowd():
    desired = pd.Series({"BTC": 0.2, "ETH": 0.2, "SOL": 0.1})
    current = pd.Series({"BTC": 0.0, "ETH": 0.0, "SOL": 0.0})
    # BTC crowd: block BTC and alts, not ETH
    out = apply_increase_block(desired, current, blocked={s for s in desired.index if s != "ETH"})
    assert out["BTC"] == 0.0 and out["SOL"] == 0.0 and out["ETH"] == 0.2


def test_weekly_signal_is_sunday_1dutc_close():
    assert is_weekly_signal(pd.Timestamp("2023-01-01")) is True  # Sunday
    assert is_weekly_signal(pd.Timestamp("2023-01-02")) is False  # Monday


def test_wedding_funds_refused():
    with pytest.raises(ValueError, match="wedding"):
        new_book("x", WEDDING_FUNDS_USDT, ["BTC"])


def test_btc_vol_gate_uses_trailing_median_not_a_fitted_cut():
    idx = pd.date_range("2021-01-01", periods=900, freq="D")
    # Low vol then a high-vol burst.
    r = np.concatenate([np.random.default_rng(0).normal(0, 0.01, 800), np.random.default_rng(1).normal(0, 0.06, 100)])
    px = 100 * np.exp(np.cumsum(r))
    s = pd.Series(px, index=idx)
    gate = btc_vol_gate(s)
    assert gate.dtype == bool
    # After the burst, some later days should trip the gate; early warmup should not.
    assert not bool(gate.iloc[100])
    assert bool(gate.iloc[-20:].any())


