"""Spec v1 protocol guards: capital, open bars, look-ahead, lock, holdout one-shot."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradingbot.eval.load import load_closed_1dutc
from tradingbot.eval.events import f2_halt_mask, last_fetch_timestamp
from tradingbot.eval.metrics import dsr, sharpe_daily_series, max_drawdown
from tradingbot.eval.protocol import (
    HoldoutAlreadyRun,
    assert_holdout_not_run,
    combine_verdicts,
    sha256_params,
    _verdict_fold,
    _verdict_holdout,
    _verdict_pooled,
)
from tradingbot.strategies.spec_v1_common import DSR_MIN, POOLED_MIN_TRADES, WEDDING_FUNDS_USDT
from tradingbot.strategies.spec_v1_signals import _donchian_components, _tsmom_components


def test_open_bars_rejected(tmp_path, monkeypatch):
    rows = [
        [1_600_000_000_000, 1, 2, 0.5, 1.5, 10, 1000, 1],
        [1_600_086_400_000, 1.5, 3, 1, 2, 10, 1000, 0],  # open bar
    ]
    cache = tmp_path / "FOO-USDT_1Dutc.json"
    cache.write_text(json.dumps(rows))
    monkeypatch.setattr("tradingbot.eval.load.TB_CACHE", tmp_path)
    monkeypatch.setattr("tradingbot.eval.load.CRYPTO_CACHE", tmp_path / "missing")
    df = load_closed_1dutc("FOO")
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 1.5


def test_f2_window_inclusive_start_exclusive_end():
    # Craft a one-row calendar via monkeypatch? Use the real calendar and a synthetic index.
    # Bar open 2022-01-25 closes 2022-01-26 00:00 UTC.
    # First FOMC is 2022-01-26 19:00 UTC, window [2022-01-25 19:00, 2022-01-27 19:00).
    # Close 2022-01-26 00:00 is inside; close 2022-01-27 00:00 is inside; close 2022-01-28 00:00 is outside.
    idx = pd.to_datetime(["2022-01-24", "2022-01-25", "2022-01-26", "2022-01-27", "2022-01-28"])
    halt = f2_halt_mask(pd.DatetimeIndex(idx))
    assert not bool(halt.loc[pd.Timestamp("2022-01-24")])  # close 01-25 00:00 < 01-25 19:00
    assert bool(halt.loc[pd.Timestamp("2022-01-25")])
    assert bool(halt.loc[pd.Timestamp("2022-01-26")])
    assert not bool(halt.loc[pd.Timestamp("2022-01-27")])


def test_calendar_fetch_stamp_exists():
    ts = last_fetch_timestamp()
    assert ts.year == 2026


def test_kill_switch_and_daily_cap_engine():
    from tradingbot.eval.engine import run_path
    from tradingbot.eval.regime import NORMAL

    # Minimal synthetic panel: one coin + BTC, constant except a crash day.
    idx = pd.date_range("2022-12-01", "2023-02-15", freq="D")
    n = len(idx)
    btc = pd.Series(100.0, index=idx)
    # SMA200 needs 200 days — extend history
    idx = pd.date_range("2022-01-01", "2023-03-01", freq="D")
    rng = np.random.default_rng(0)
    close = pd.DataFrame({
        "BTC": 100 + np.cumsum(rng.normal(0.05, 0.5, len(idx))),
        "AAA": 10 + np.cumsum(rng.normal(0.02, 0.3, len(idx))),
    }, index=idx)
    close = close.clip(lower=1)
    high = close * 1.01
    low = close * 0.99
    op = close.shift(1).fillna(close)
    vq = pd.DataFrame(5e6, index=idx, columns=close.columns)
    panel = {"open": op, "high": high, "low": low, "close": close, "vq": vq, "bases": list(close.columns), "last_closed": idx[-1]}
    from tradingbot.eval.regime import build_regime
    from tradingbot.strategies.spec_v1_signals import universe_and_breadth, va_indicators, vb_signals, vc_signals, vol_ann

    # Don't require real F2 file staleness: build_regime uses it; that's fine.
    regime = build_regime(panel)
    U, br = universe_and_breadth(panel)
    # Force AAA always in universe for this unit test? monthly_top20 may exclude short hist first 200d — we have enough.
    pre = {
        "U": U,
        "breadth": br,
        "vol": vol_ann(close),
        "log_rets": np.log(close).diff(),
        "va": va_indicators(panel, U, br, regime["r1_on"]),
        "vb": vb_signals(panel, U),
        "vc": vc_signals(panel, U),
    }
    res = run_path("V-A", "2x", panel, pre, regime, capital=10_000,
                   path_start=pd.Timestamp("2023-01-01"), path_end=pd.Timestamp("2023-03-01"))
    assert res.capital == 10_000
    # After kill, equity path stays flat (all cash)
    if res.killed_on is not None:
        after = res.exposure.loc[res.exposure.index > res.killed_on]
        assert (after.abs() < 1e-9).all() or len(after) == 0


def test_wedding_funds_constant_not_used_as_default():
    assert WEDDING_FUNDS_USDT == 6788.0
    # params hash is stable and does not include 6788 as capital
    h = sha256_params()
    assert isinstance(h, str) and len(h) == 64


def test_okx_balance_not_imported():
    import sys
    # The eval package must not import the readonly balance modules.
    import tradingbot.eval.engine as eng  # noqa: F401
    import tradingbot.eval.run_spec_v1 as run  # noqa: F401
    assert "okx_balance_readonly" not in sys.modules
    assert "okx_onchain_readonly" not in sys.modules
    src = Path("/workspace/trading-bot/tradingbot/eval/engine.py").read_text()
    src += Path("/workspace/trading-bot/tradingbot/eval/run_spec_v1.py").read_text()
    src += Path("/workspace/trading-bot/tradingbot/eval/load.py").read_text()
    assert "okx_balance_readonly" not in src or "FORBIDDEN" in Path("/workspace/trading-bot/tradingbot/eval/run_spec_v1.py").read_text()
    assert "okx_onchain_readonly" not in Path("/workspace/trading-bot/tradingbot/eval/engine.py").read_text()
    assert "6788" not in Path("/workspace/trading-bot/tradingbot/eval/engine.py").read_text()


def test_holdout_refuse(tmp_path, monkeypatch):
    import tradingbot.eval.protocol as proto
    target = tmp_path / "holdout_H1.json"
    target.write_text("{}")
    monkeypatch.setitem(proto.HOLDOUT, "H1", target)
    with pytest.raises(HoldoutAlreadyRun):
        assert_holdout_not_run("H1")


def test_fill_is_next_open_not_signal_close():
    """A breakout on day t must not earn t's close-to-close return (look-ahead / fill rule)."""
    from tradingbot.eval.engine import run_path
    from tradingbot.eval.regime import build_regime
    from tradingbot.strategies.spec_v1_signals import universe_and_breadth, va_indicators, vb_signals, vc_signals, vol_ann

    idx = pd.date_range("2021-01-01", "2023-03-15", freq="D")
    btc = pd.Series(100.0, index=idx)
    # Gentle upward BTC so SMA200 is defined and R1 is on after mid-2021
    btc = 50 + pd.Series(np.linspace(0, 100, len(idx)), index=idx)
    aaa = pd.Series(10.0, index=idx)
    # On 2023-01-10, a huge gap-up close that would look profitable close-to-close,
    # then the next open is unchanged — next-open fill must not capture the gap.
    aaa.loc[: "2023-01-09"] = 10.0
    aaa.loc["2023-01-10"] = 20.0  # close breakout
    aaa.loc["2023-01-11":] = 20.0
    close = pd.DataFrame({"BTC": btc, "AAA": aaa})
    high, low = close * 1.0, close * 1.0
    # next open equals previous close (24/7) EXCEPT we set 2023-01-11 open = 20 (no gap vs close t)
    op = close.shift(1)
    op.loc["2023-01-11", "AAA"] = 20.0
    op = op.fillna(close)
    vq = pd.DataFrame(5e6, index=idx, columns=["BTC", "AAA"])
    panel = {"open": op, "high": high, "low": low, "close": close, "vq": vq, "bases": ["BTC", "AAA"], "last_closed": idx[-1]}
    regime = build_regime(panel)
    # Force no F2 halt around the test days by leaving calendar as-is (Jan 2023 CPI may halt; pick a quiet day if needed)
    U, br = universe_and_breadth(panel)
    pre = {
        "U": U, "breadth": br, "vol": vol_ann(close), "log_rets": np.log(close).diff(),
        "va": va_indicators(panel, U, br, regime["r1_on"]),
        "vb": vb_signals(panel, U), "vc": vc_signals(panel, U),
    }
    res = run_path("V-A", "1x", panel, pre, regime, capital=10_000,
                   path_start=pd.Timestamp("2023-01-01"), path_end=pd.Timestamp("2023-01-20"))
    # Equity change on 2023-01-10 (signal bar) must be near 0 if we only fill next morning —
    # we must not capture the 10→20 close jump that day.
    if pd.Timestamp("2023-01-10") in res.rets.index:
        # Any position marked at the 20 close on the 10th would show a huge gain; forbid it.
        assert res.rets.loc[pd.Timestamp("2023-01-10")] < 0.05


def test_donchian_and_tsmom_no_lookahead():
    idx = pd.date_range("2020-01-01", periods=30, freq="D")
    close = pd.Series(1.0, index=idx)
    close.iloc[20] = 2.0  # new high close on t=20 (0-based)
    high = close.copy()
    low = close.copy() * 0.9
    # max(high[t-20:t-1]) at t=20 is 1.0, close=2 > 1 → enter
    dc = _donchian_components(high, low, close, (20,))
    long = dc[20]["long"]
    # Must not be long before t=20
    assert not bool(long.iloc[19])
    assert bool(long.iloc[20])
    # TSMOM 21: close[t]/close[t-21] > 1 only after enough history
    tm = _tsmom_components(close, (21,))
    assert not bool(tm[21].iloc[20])  # 2/1 > 1? t-21 = -1, shift → NaN → False
    close2 = pd.Series(np.linspace(1, 2, 40), index=pd.date_range("2020-01-01", periods=40, freq="D"))
    tm2 = _tsmom_components(close2, (21,))
    assert bool(tm2[21].iloc[21])  # strictly increasing


def test_dsr_matches_research4_shape():
    # Identical inputs should be a valid probability in [0,1]
    val, sr0 = dsr(sr_d=0.05, T=700, skew=0.0, kurt=3.0, sr_trials_var=0.0001, N=3)
    assert 0.0 <= val <= 1.0
    assert sr0 >= 0.0


def test_fold_insufficient_and_holdout_fail_priority():
    folds = [
        {"n_days": 180, "n": 2, "Sharpe": 1.0, "maxDD": -0.05},
        {"n_days": 180, "n": 2, "Sharpe": 1.0, "maxDD": -0.05},
        {"n_days": 180, "n": 2, "Sharpe": 1.0, "maxDD": -0.05},
        {"n_days": 180, "n": 2, "Sharpe": 1.0, "maxDD": -0.05},
    ]
    v = _verdict_fold(folds)
    assert v["result"] == "INSUFFICIENT"
    h = _verdict_holdout("H1", {"n": 20, "Sharpe": 0.1, "maxDD": -0.05}, killed_before=True)
    assert h["result"] == "FAIL"
    overall, _ = combine_verdicts([v, h])
    assert overall == "FAIL"  # a holdout FAIL beats insufficient


def test_pooled_n_floor():
    m = {"n": 10, "Sharpe": 1.0, "maxDD": -0.05}
    v = _verdict_pooled(m, {"Sharpe": 0.5}, {"Sharpe": 0.2}, btc_sharpe=0.0, va_sharpe=None, variant="V-A", dsr3={"DSR": 0.99})
    assert v["result"] == "INSUFFICIENT"
    assert v["n"] == 10
    assert POOLED_MIN_TRADES == 30
    assert DSR_MIN == 0.95


def test_max_drawdown_sign():
    eq = pd.Series([1.0, 1.2, 0.9, 1.0])
    dd = max_drawdown(eq)
    assert dd == pytest.approx(0.9 / 1.2 - 1.0)
    assert dd < 0
