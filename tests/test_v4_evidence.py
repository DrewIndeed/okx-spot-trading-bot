import numpy as np
import pandas as pd
import pytest

from tradingbot.eval import v4_evidence as E
from tradingbot.eval.v2_engine import BookState


def _st():
    return BookState(name="t", cash=10000.0, qty={"BTC": 0.0, "SOL": 0.0}, peak=10000.0, equity=10000.0)


def test_slip_fill_charges_extra_only_on_alts():
    st = _st()
    fill = E.make_slip_fill(extra=0.005)
    fill(st, {"BTC": 0.5, "SOL": 0.5}, {"BTC": 100.0, "SOL": 10.0}, pd.Timestamp("2020-01-02"), 0.004, block_new=False, flatten=False)
    assert st.trades_open["BTC"]["entry_px"] == pytest.approx(100.0 * 1.004)
    assert st.trades_open["SOL"]["entry_px"] == pytest.approx(10.0 * 1.009)
    assert set(st.qty) == {"BTC", "SOL"} and st.qty["SOL"] == pytest.approx(500.0)


def test_slip_fill_zero_extra_matches_original_fill():
    from tradingbot.eval.v2_engine import _fill_to_weights
    a, b = _st(), _st()
    args = ({"BTC": 0.3, "SOL": 0.6}, {"BTC": 100.0, "SOL": 10.0}, pd.Timestamp("2020-01-02"), 0.004)
    _fill_to_weights(a, *args, block_new=False, flatten=False)
    E.make_slip_fill(0.0)(b, *args, block_new=False, flatten=False)
    assert a.cash == pytest.approx(b.cash, abs=1e-9) and a.qty == pytest.approx(b.qty)


def test_bootstrap_identical_books_gives_zero_gap_and_is_seeded():
    r = pd.Series(np.random.default_rng(1).normal(0.001, 0.02, 400))
    out = E.bootstrap_sharpe_gap(r, r, block=20, n=200, seed=7)
    assert out["gap"] == 0 and out["ci95"] == [0.0, 0.0]
    x = r + 0.002
    o1 = E.bootstrap_sharpe_gap(x, r, block=20, n=200, seed=7)
    o2 = E.bootstrap_sharpe_gap(x, r, block=20, n=200, seed=7)
    assert o1 == o2 and o1["gap"] > 0 and o1["p_one_sided"] < 0.5


def test_rolling_windows_quarterly_12m():
    w = E.rolling_windows("2021-07-01", "2026-11-10")
    assert len(w) == 18
    assert w[0] == (pd.Timestamp("2021-07-01"), pd.Timestamp("2022-06-30"))
    assert w[-1] == (pd.Timestamp("2025-10-01"), pd.Timestamp("2026-09-30"))


def test_symbol_filter_excludes_stables_and_leveraged():
    assert E.keep_symbol("BCCUSDT") == "BCC"
    assert E.keep_symbol("TUSDUSDT") is None and E.keep_symbol("BTCUPUSDT") is None
    assert E.keep_symbol("XRPBEARUSDT") is None and E.keep_symbol("BTCBUSD") is None


def test_kline_rows_to_frame_uses_quote_volume_and_cutoff():
    rows = [[1577836800000, "1", "2", "0.5", "1.5", "10", 1577923199999, "15", 5, "0", "0", "0"],
            [1601510400000, "1", "2", "0.5", "1.5", "10", 1601596799999, "15", 5, "0", "0", "0"]]
    df = E.klines_to_frame(rows, cutoff="2020-09-30")
    assert len(df) == 1 and df.iloc[0]["volume_quote"] == 15.0 and df.iloc[0]["confirm"] == 1
