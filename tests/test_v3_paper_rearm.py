"""v3 paper kill-switch re-arm: frozen rule 're-arm after 30 flat days AND BTC > SMA200'.

The paper runner must supply BTC's SMA200 (closed 1Dutc bars, no lookahead) to the frozen
engine; the frozen H1/H4/H5 precompute does not include it, so a killed book could never re-arm.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.eval import v3_paper
from tradingbot.eval.v2_engine import new_book
from tradingbot.eval.v3_engine import step_generic


class _FrozenLikeH:
    """Mimics frozen precompute_vc: no sma200 / sma200_btc keys."""

    symbols = ["BTC"]

    def precompute(self, panel):
        return {"vc": "frozen"}

    @staticmethod
    def target_fn(date, panel, pre, cur, state):
        return pd.Series({"BTC": 0.0})


def _panel(n=260):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    px = pd.DataFrame({"BTC": np.arange(n, dtype=float) + 100.0}, index=idx)  # rising: BTC > SMA200
    return {"open": px.copy(), "close": px.copy()}


def _killed_flat_book():
    st = new_book("H5", 10000.0, ["BTC"])
    st.killed, st.killed_on = True, "2025-07-01"
    return st


def _run(pre, panel, start=210, days=31):
    st = _killed_flat_book()
    row = pd.Series({"macroy_state": "NORMAL"})
    for dt in panel["close"].index[start : start + days]:
        step_generic(st, dt, panel, pre, row, _FrozenLikeH.target_fn, ["BTC"], cost=0.004)
    return st


def test_paper_pre_rearms_after_30_flat_days_when_btc_above_sma200():
    panel = _panel()
    st = _run(v3_paper.paper_pre(_FrozenLikeH(), panel), panel)
    assert not st.killed
    assert len(st.rearmed_on) == 1
    assert st.rearmed_on[0] == str(panel["close"].index[210 + 29].date())  # 30th flat day


def test_frozen_precompute_alone_never_rearms_documents_defect():
    panel = _panel()
    st = _run(_FrozenLikeH().precompute(panel), panel)
    assert st.killed and st.rearmed_on == []


def test_paper_pre_keeps_frozen_keys_and_btc_sma200_has_no_lookahead():
    panel = _panel()
    pre = v3_paper.paper_pre(_FrozenLikeH(), panel)
    assert pre["vc"] == "frozen"
    s = pre["sma200_btc"]
    c = panel["close"]["BTC"]
    assert np.isnan(s.iloc[198]) and s.iloc[199] == c.iloc[:200].mean()
    future = {k: v.copy() for k, v in panel.items()}
    future["close"].iloc[230:, 0] = 1.0  # mutate the future only
    s2 = v3_paper.paper_pre(_FrozenLikeH(), future)["sma200_btc"]
    pd.testing.assert_series_equal(s.iloc[:230], s2.iloc[:230])


def test_paper_pre_does_not_rearm_when_btc_below_sma200():
    panel = _panel()
    falling = pd.DataFrame({"BTC": 1000.0 - np.arange(260, dtype=float)}, index=panel["close"].index)
    panel = {"open": falling.copy(), "close": falling.copy()}
    st = _run(v3_paper.paper_pre(_FrozenLikeH(), panel), panel)
    assert st.killed and st.rearmed_on == []


def test_paper_runner_steps_books_with_paper_pre_not_bare_precompute():
    import inspect

    src = inspect.getsource(v3_paper)
    assert "pres[hid] = paper_pre(hyps[hid], panel)" in src
    assert "pres[hid] = hyps[hid].precompute(panel)" not in src
