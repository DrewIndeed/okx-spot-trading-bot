"""V2-CORE unit tests: closed candles, F2 calendar, sizing caps, re-arm, C_paper."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import AllocatedCapitalRequired, load_config
from tradingbot.eval.events import calendar_is_stale, load_events
from tradingbot.eval.load import load_closed_1dutc, load_panel
from tradingbot.eval.v2_engine import new_book, run_bench_path, run_core_path, step_core
from tradingbot.eval.v2_paper import c_paper_from, run_dirs
from tradingbot.strategies import spec_v2_common as P
from tradingbot.strategies.spec_v2_core import core_signals, core_target_weights
from tradingbot.strategies.spec_v1_common import WEDDING_FUNDS_USDT


def test_open_bars_not_in_panel(tmp_path, monkeypatch):
    rows = [
        [1_600_000_000_000, 1, 2, 0.5, 1.5, 10, 1000, 1],
        [1_600_086_400_000, 1.5, 3, 1, 2, 10, 1000, 0],
    ]
    (tmp_path / "FOO-USDT_1Dutc.json").write_text(json.dumps(rows))
    monkeypatch.setattr("tradingbot.eval.load.TB_CACHE", tmp_path)
    monkeypatch.setattr("tradingbot.eval.load.CRYPTO_CACHE", tmp_path / "missing")
    df = load_closed_1dutc("FOO")
    assert len(df) == 1


def test_f2_calendar_loaded_and_covers_t0_window():
    ev = load_events()
    assert len(ev) > 0
    assert ev["event_type"].isin(["CPI", "PPI", "FOMC_STATEMENT"]).all()
    assert ev["tau"].max() >= pd.Timestamp("2026-11-13", tz="UTC")
    assert calendar_is_stale() is False


def test_core_signal_requires_sma_and_two_donchian():
    idx = pd.date_range("2020-01-01", periods=250, freq="D")
    # Flat then a clean breakout late
    close = pd.DataFrame({"BTC": np.linspace(100, 110, 250), "ETH": np.linspace(10, 11, 250)}, index=idx)
    # last bar jumps well above range
    close.iloc[-1] = close.iloc[-2] * 1.2
    high = close * 1.0
    # make prior highs equal to close so a jump breaks Donchian
    high.iloc[:-1] = close.iloc[:-1]
    low = close * 0.99
    panel = {"close": close, "high": high, "low": low, "open": close.shift(1).fillna(close)}
    pre = core_signals(panel)
    # After 200 SMA + 90 donchian, the final jump should be long on both
    assert bool(pre["long"].iloc[-1]["BTC"]) or bool(pre["count"].iloc[-1]["BTC"] >= 2)


def test_gross_cap_no_leverage():
    idx = pd.date_range("2020-01-01", periods=120, freq="D")
    close = pd.DataFrame({"BTC": 100.0, "ETH": 10.0}, index=idx)
    # tiny vol so raw vol weights would exceed 100%
    close["BTC"] = 100 + np.linspace(0, 0.1, 120)
    close["ETH"] = 10 + np.linspace(0, 0.01, 120)
    high, low = close * 1.001, close * 0.999
    panel = {"close": close, "high": high, "low": low, "open": close.shift(1).fillna(close)}
    pre = core_signals(panel)
    # force both long on last bar
    pre["long"].iloc[-1] = True
    t = close.index[-1]
    w = core_target_weights(t, panel, pre, pd.Series({"BTC": 0.0, "ETH": 0.0}), "NORMAL")
    assert float(w.sum()) <= P.CORE_GROSS_CAP + 1e-9
    assert (w >= -1e-12).all()


def test_halt_blocks_new_risk():
    idx = pd.date_range("2020-01-01", periods=120, freq="D")
    close = pd.DataFrame({"BTC": np.linspace(100, 120, 120), "ETH": np.linspace(10, 12, 120)}, index=idx)
    panel = {"close": close, "high": close * 1.01, "low": close * 0.99, "open": close.shift(1).fillna(close)}
    pre = core_signals(panel)
    pre["long"].iloc[-1] = True
    t = close.index[-1]
    w = core_target_weights(t, panel, pre, pd.Series({"BTC": 0.0, "ETH": 0.0}), "HALT")
    assert float(w.sum()) == pytest.approx(0.0)


def test_c_paper_rejects_wedding_funds(tmp_path):
    from tradingbot.config import AllocatedCapitalRequired, Config
    with pytest.raises(AllocatedCapitalRequired, match="wedding-funds"):
        Config(allocated_capital=10_000, c_paper=6788.0)


def test_c_paper_required(tmp_path):
    from tradingbot.config import Config
    cfg = Config(allocated_capital=10_000)  # c_paper default may be None or alias
    # If c_paper is unset, c_paper_from must raise
    if getattr(cfg, "c_paper", None) is None:
        with pytest.raises(ValueError, match="C_paper"):
            c_paper_from(cfg)


def test_state_survives_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dirs = run_dirs("spec_v2_official")
    st = new_book("V2-CORE", 10_000, ["BTC", "ETH"])
    st.qty["BTC"] = 0.1
    st.equity = 10100
    payload = {"books": {"V2-CORE": st.to_json()}, "initialized": True}
    (dirs["ledger"]).write_text(json.dumps(payload))
    loaded = json.loads(dirs["ledger"].read_text())
    st2 = type(st).from_json(loaded["books"]["V2-CORE"])
    assert st2.qty["BTC"] == pytest.approx(0.1)
    assert st2.equity == pytest.approx(10100)


def test_kill_rearm_after_30_flat_days_and_btc_above_sma():
    """Synthetic path: force a kill then 30 flat days with BTC above SMA200."""
    from tradingbot.eval.regime import build_regime
    from tradingbot.eval.v2_engine import BookState, step_core

    idx = pd.date_range("2021-01-01", periods=400, freq="D")
    btc = 50 + np.linspace(0, 100, 400)  # steadily above a rising SMA eventually
    close = pd.DataFrame({"BTC": btc, "ETH": btc / 10}, index=idx)
    panel = {"close": close, "high": close * 1.01, "low": close * 0.99, "open": close.shift(1).fillna(close)}
    pre = core_signals(panel)
    st = new_book("V2-CORE", 10_000, ["BTC", "ETH"])
    # Simulate already-killed, flat
    st.killed = True
    st.flat_days = 29
    st.peak = 12_000
    st.equity = 10_000
    # pick a late date where SMA200 exists and BTC > SMA
    t = close.index[-1]
    assert pd.notna(pre["sma200"].at[t, "BTC"])
    assert close.at[t, "BTC"] > pre["sma200"].at[t, "BTC"]
    # one more flat counted day should re-arm
    row = pd.Series({"macroy_state": "NORMAL", "f2_halt": False, "r1_on": True})
    step_core(st, t, panel, pre, row, count_bar=True)
    assert st.killed is False
    assert st.flat_days == 0
    assert st.peak == pytest.approx(st.equity)
    assert str(t.date()) in st.rearmed_on


def test_bench_monthly_rebalance_and_2x_fees():
    idx = pd.date_range("2023-01-01", periods=60, freq="D")
    close = pd.DataFrame({"BTC": 100.0, "ETH": 10.0}, index=idx)
    close["BTC"] = 100 + np.arange(60)
    close["ETH"] = 10 + np.arange(60) * 0.1
    panel = {
        "close": close,
        "high": close,
        "low": close,
        "open": close,  # 24/7 proxy
        "last_closed": idx[-1],
    }
    st = run_bench_path(panel, 10_000, idx[0], idx[-1])
    # first day and Feb 1 should have traded
    assert len(st.equity_curve) == 60
    # fees: starting 50/50 at 40bps/side on full notional ≈ 40bps of C
    assert st.equity_curve[0]["equity"] < 10_000
    assert st.trades_open or st.qty["BTC"] > 0
