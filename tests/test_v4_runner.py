import numpy as np
import pandas as pd

from tradingbot.eval.run_spec_v4 import book_pre


class _H:
    def precompute(self, panel):
        return {"x": 1}


def test_book_pre_supplies_btc_sma200_so_kill_switch_can_rearm():
    idx = pd.date_range("2020-01-01", periods=250, freq="D")
    panel = {"close": pd.DataFrame({"BTC": np.arange(250, dtype=float) + 1}, index=idx)}
    pre = book_pre(_H(), panel)
    assert pre["x"] == 1
    assert np.isnan(pre["sma200_btc"].iloc[198]) and pre["sma200_btc"].iloc[199] == np.arange(1, 201).mean()


def test_metrics_reports_finite_dsr():
    from tradingbot.eval.run_spec_v4 import metrics
    idx = pd.date_range("2020-01-01", periods=400, freq="D")
    r = pd.Series(np.random.default_rng(1).normal(0.001, 0.01, 400), index=idx)
    eq = 10000 * (1 + r).cumprod()
    m = metrics(eq, r, [], r * 0 + 0.5, r * 0 + 0.01, [0.01, 0.03, 0.05])
    assert m["DSR_N16"] is not None and 0.0 <= m["DSR_N16"] <= 1.0
    assert m["DSR_N51"] <= m["DSR_N16"]
