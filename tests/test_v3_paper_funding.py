"""TDD tests for the H1 paper funding seed (archive module 3 + REST merge).

Plumbing only; does not import or modify frozen strategy logic beyond
calling the existing parsers / crowding_flags / trailing_p90 helpers.
"""
from __future__ import annotations

import pandas as pd
import pytest

from tradingbot.eval.v3_paper import T0_BAR


T0_CLOSE = T0_BAR + pd.Timedelta(days=1)  # 2026-11-12 00:00 UTC


def test_merge_seed_and_rest_dedupes_by_funding_time():
    from tradingbot.eval.v3_paper_funding import merge_seed_and_rest

    t1 = pd.Timestamp("2026-10-01 08:00:00")
    t2 = pd.Timestamp("2026-10-01 16:00:00")
    t3 = pd.Timestamp("2026-10-02 00:00:00")
    archive = {
        "BTC-USDT-SWAP": pd.Series({t1: 0.0001, t2: 0.0002}),
    }
    rest = {
        "BTC-USDT-SWAP": pd.Series({t2: 0.0009, t3: 0.0003}),  # t2 overlaps
        "ETH-USDT-SWAP": pd.Series({t3: 0.0004}),
    }
    merged = merge_seed_and_rest(archive, rest)
    btc = merged["BTC-USDT-SWAP"]
    assert list(btc.index) == [t1, t2, t3]
    assert float(btc.loc[t2]) == 0.0009  # REST wins on the same fundingTime
    assert float(btc.loc[t1]) == 0.0001
    assert float(btc.loc[t3]) == 0.0003
    assert list(merged["ETH-USDT-SWAP"].index) == [t3]


def test_seed_excludes_month_not_yet_published_at_signal_time():
    from tradingbot.eval.v3_paper_funding import archive_series_asof

    early = pd.Timestamp("2026-09-01 08:00:00")
    late = pd.Timestamp("2026-11-05 08:00:00")  # would be in a Nov-2026 zip
    chunks = [
        {
            "inst": "BTC-USDT-SWAP",
            "month": "2026-09",
            "usable_at": pd.Timestamp("2026-10-01 00:00:00"),  # UTC+8 month close → next month start as naive UTC-equivalent for the test
            "series": pd.Series({early: 0.0001}),
        },
        {
            "inst": "BTC-USDT-SWAP",
            "month": "2026-11",
            "usable_at": pd.Timestamp("2026-12-01 00:00:00"),
            "series": pd.Series({late: 0.0099}),
        },
    ]
    # Signal at T0 close: Nov 2026 month is not yet published.
    out = archive_series_asof(chunks, T0_CLOSE)
    btc = out["BTC-USDT-SWAP"]
    assert early in btc.index
    assert late not in btc.index
    assert 0.0099 not in set(btc.values)


def test_missing_forward_rest_day_sets_filter_off_even_if_archive_has_a_print():
    from tradingbot.eval.v3_paper_funding import crowd_for_paper

    # A forward day (T0 bar open) whose close is T0_CLOSE.
    bar = T0_BAR
    # Archive has a print just before the close (would look like a rate if we backfilled).
    archive_print = T0_CLOSE - pd.Timedelta(hours=8)
    history = pd.date_range(T0_CLOSE - pd.Timedelta(days=120), archive_print, freq="8h")
    merged = {
        "BTC-USDT-SWAP": pd.Series(0.0002, index=history),
        "ETH-USDT-SWAP": pd.Series(0.0002, index=history),
    }
    # REST has history up to the previous day only — the T0 close day is missing.
    rest_end = T0_CLOSE - pd.Timedelta(days=4)
    rest_idx = pd.date_range(T0_CLOSE - pd.Timedelta(days=40), rest_end, freq="8h")
    rest = {
        "BTC-USDT-SWAP": pd.Series(0.0002, index=rest_idx),
        "ETH-USDT-SWAP": pd.Series(0.0002, index=rest_idx),
    }
    crowd, events = crowd_for_paper(merged, rest, pd.DatetimeIndex([bar]))
    assert bar in crowd.index
    row = crowd.loc[bar]
    assert bool(row["btc_skip"]) is True
    assert bool(row["eth_skip"]) is True
    assert bool(row["btc_crowd"]) is False
    assert bool(row["eth_crowd"]) is False
    assert any(e.get("type") == "h1_funding_missing" and e.get("action") == "filter_OFF" for e in events)


def test_effective_window_at_t0_is_about_365_days():
    from tradingbot.eval.v3_paper_funding import trailing_window_stats

    # 8h prints covering 400 days before T0 close.
    start = T0_CLOSE - pd.Timedelta(days=400)
    idx = pd.date_range(start, T0_CLOSE, freq="8h")
    s = pd.Series(0.0001, index=idx)
    stats = trailing_window_stats(s, T0_CLOSE, days=365)
    assert stats["n_prints"] >= 365 * 3 - 2  # ~3 prints/day
    assert stats["span_days"] >= 360
    # First print used is near T0-365d, not ~90d (the REST-only span).
    assert stats["actual_start"] <= T0_CLOSE - pd.Timedelta(days=360)
    assert stats["requested_start"] == T0_CLOSE - pd.Timedelta(days=365)
