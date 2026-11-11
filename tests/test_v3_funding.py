"""Funding parse, look-ahead, and stale-skip tests."""

from __future__ import annotations

import pandas as pd

from tradingbot.eval.v3_funding import (
    crowding_flags,
    last_print_at_or_before,
    parse_archive_funding_time_ms,
    parse_rest_funding_time_ms,
    trailing_p90,
)


def test_archive_funding_time_is_unix_instant_utc8_calendar():
    # First row of the probed 2023-01 BTC zip: 2023-01-01 00:00 UTC+8 = 2022-12-31 16:00 UTC.
    ts = parse_archive_funding_time_ms(1672502400000)
    assert ts == pd.Timestamp("2022-12-31 16:00:00")
    # REST uses the same Unix-instant convention (no extra 8h shift).
    assert parse_rest_funding_time_ms(1672502400000) == ts


def test_no_lookahead_uses_print_at_or_before_close():
    s = pd.Series(
        [0.01, 0.02, 0.03],
        index=pd.to_datetime(["2023-01-01 16:00", "2023-01-02 00:00", "2023-01-02 08:00"]),
    )
    # 1Dutc 2023-01-01 closes 2023-01-02 00:00 UTC; the 08:00 print is after close.
    ts, rate = last_print_at_or_before(s, pd.Timestamp("2023-01-02 00:00"))
    assert ts == pd.Timestamp("2023-01-02 00:00")
    assert rate == 0.02


def test_stale_skips_filter_and_does_not_invent_rate():
    # One print, then a 10-day gap.
    s = pd.Series([0.05], index=pd.to_datetime(["2023-01-01 16:00"]))
    idx = pd.to_datetime(["2023-01-01", "2023-01-12"])
    funding = {"BTC-USDT-SWAP": s, "ETH-USDT-SWAP": s}
    flags = crowding_flags(funding, idx)
    # 2023-01-01 close is 2023-01-02; print is 8h earlier, not stale, but p90 needs 90 prints.
    assert bool(flags.loc[idx[0], "btc_skip"]) is False or flags.loc[idx[0], "btc_p90"] is None
    # 2023-01-12 is stale >3 days → skip filter, do not crowd.
    assert bool(flags.loc[idx[1], "btc_stale"]) is True
    assert bool(flags.loc[idx[1], "btc_skip"]) is True
    assert bool(flags.loc[idx[1], "btc_crowd"]) is False


def test_trailing_p90_requires_min_periods():
    idx = pd.date_range("2023-01-01", periods=50, freq="8h")
    s = pd.Series(0.001, index=idx)
    assert trailing_p90(s, idx[-1], days=365, min_periods=90) is None
    idx = pd.date_range("2023-01-01", periods=120, freq="8h")
    s = pd.Series(list(range(120)), index=idx)
    q = trailing_p90(s, idx[-1], days=365, min_periods=90)
    assert q is not None and q == float(s.quantile(0.90))
