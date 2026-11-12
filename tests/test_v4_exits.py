"""v4 exit/entry overlay tests: next-bar fills, no lookahead, exit-before-entry, lockout, scale-out, filters."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.eval.v2_engine import new_book
from tradingbot.eval.v4_exits import V4Config, fill_exits_then_entries, make_v4_target, run_v4_path

COST = 0.004


def _panel(closes: dict) -> dict:
    idx = pd.date_range("2020-01-01", periods=len(next(iter(closes.values()))), freq="D")
    C = pd.DataFrame(closes, index=idx, dtype=float)
    O = C.shift(1).fillna(C.iloc[0])
    return {"open": O, "close": C, "high": np.maximum(O, C) + 0.5, "low": np.minimum(O, C) - 0.5,
            "vq": C * 0 + 1e9, "bases": list(C.columns)}


def _regime(panel):
    return pd.DataFrame({"macroy_state": "NORMAL"}, index=panel["close"].index)


def _const(w):
    def fn(t, panel, pre, cur, state):
        return pd.Series({s: float(w.get(s, 0.0)) for s in panel["close"].columns})
    return fn


def _ramp_then_drop(n=260, k=230, drop=12.0):
    up = np.linspace(100, 200, n)
    up[k:] = up[k:] - drop  # step down at bar k
    return up


def _run(panel, cfg, base, start_i=210):
    tf = make_v4_target(base, cfg, panel)
    idx = panel["close"].index
    return run_v4_path("t", list(panel["close"].columns), tf, panel, {}, _regime(panel), 10000.0, idx[start_i], idx[-1], cost=COST)


def test_trailing_exit_fills_next_open_not_signal_bar():
    p = _panel({"BTC": _ramp_then_drop()})
    st = _run(p, V4Config(trail_atr=3.0), _const({"BTC": 0.5}))
    idx = p["close"].index
    ex = [t for t in st.trades_closed if t["symbol"] == "BTC"]
    assert ex, "trailing stop never fired"
    assert pd.Timestamp(ex[0]["exit_date"]) == idx[231]  # signal at close of bar 230, fill at open of 231
    assert ex[0]["exit_px"] == pytest.approx(p["open"]["BTC"].iloc[231] * (1 - COST))


def test_lockout_blocks_reentry_while_base_still_long():
    p = _panel({"BTC": _ramp_then_drop()})
    st = _run(p, V4Config(trail_atr=3.0), _const({"BTC": 0.5}))
    assert len([t for t in st.trades_closed if t["symbol"] == "BTC"]) == 1
    assert abs(st.qty.get("BTC", 0.0)) < 1e-12


def test_reentry_allowed_after_base_resets():
    p = _panel({"BTC": _ramp_then_drop()})
    idx = p["close"].index
    def base(t, panel, pre, cur, state):
        return pd.Series({"BTC": 0.0 if t in (idx[240],) else 0.5})
    st = _run(p, V4Config(trail_atr=3.0), base)
    assert st.qty.get("BTC", 0.0) > 0


def test_no_lookahead_future_bars_do_not_change_past():
    a = _ramp_then_drop()
    b = a.copy(); b[245:] = 20.0  # crash only after cut
    pa, pb = _panel({"BTC": a, "ETH": a[::-1].copy()}), _panel({"BTC": b, "ETH": a[::-1].copy()})
    cfg = V4Config(trail_atr=3.0, scale_atr=3.0, time_days=10, buy_trend=True, vol_shock=1.5)
    ea = [r["equity"] for r in _run(pa, cfg, _const({"BTC": 0.4, "ETH": 0.4})).equity_curve]
    eb = [r["equity"] for r in _run(pb, cfg, _const({"BTC": 0.4, "ETH": 0.4})).equity_curve]
    cut = 245 - 210  # equity rows before bar 245 (fills at 245 use open=close[244])
    assert ea[:cut] == pytest.approx(eb[:cut])


def test_exit_before_entry_in_same_bar():
    st = new_book("x", 10000.0, ["A", "B"])
    st.qty["A"] = 100.0; st.cash = 0.0; st.equity = 10000.0
    seen = []
    fill_exits_then_entries(st, {"A": 0.0, "B": 0.99}, {"A": 100.0, "B": 50.0}, pd.Timestamp("2020-01-02"), COST,
                            block_new=False, flatten=False, _probe=lambda s: seen.append((dict(s.qty), s.cash)))
    (q1, cash1) = seen[0]
    assert q1["A"] == 0.0 and q1.get("B", 0.0) == 0.0 and cash1 > 0  # exits done, no entry yet
    assert st.qty["B"] > 0 and st.qty["A"] == 0.0


def test_scale_out_halves_after_gain_in_atr():
    p = _panel({"BTC": np.linspace(100, 200, 260)})
    tf = make_v4_target(_const({"BTC": 0.5}), V4Config(scale_atr=3.0), p)
    idx = p["close"].index
    cur = pd.Series({"BTC": 0.0}); w = None
    for i in range(200, 230):
        w = tf(idx[i], p, {}, cur, "NORMAL"); cur = pd.Series({"BTC": 0.5 if i == 200 else float(w["BTC"])})
    assert w["BTC"] == pytest.approx(0.25)


def test_buy_trend_blocks_new_entry_below_sma_but_keeps_holding():
    down = np.linspace(200, 100, 260)
    p = _panel({"BTC": down})
    tf = make_v4_target(_const({"BTC": 0.5}), V4Config(buy_trend=True), p)
    t = p["close"].index[230]
    assert tf(t, p, {}, pd.Series({"BTC": 0.0}), "NORMAL")["BTC"] == 0.0
    tf2 = make_v4_target(_const({"BTC": 0.5}), V4Config(buy_trend=True), p)
    assert tf2(t, p, {}, pd.Series({"BTC": 0.5}), "NORMAL")["BTC"] == pytest.approx(0.5)


def test_vol_shock_halves_and_blocks_increase():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 600); r[580:] = rng.normal(0, 0.08, 20)
    p = _panel({"BTC": 100 * np.exp(np.cumsum(r))})
    tf = make_v4_target(_const({"BTC": 0.5}), V4Config(vol_shock=1.5), p)
    t = p["close"].index[-1]
    assert tf(t, p, {}, pd.Series({"BTC": 0.5}), "NORMAL")["BTC"] == pytest.approx(0.25)
    assert make_v4_target(_const({"BTC": 0.5}), V4Config(vol_shock=1.5), p)(t, p, {}, pd.Series({"BTC": 0.0}), "NORMAL")["BTC"] == 0.0


def test_time_exit_when_no_progress():
    p = _panel({"BTC": np.full(260, 100.0)})
    st = _run(p, V4Config(time_days=10), _const({"BTC": 0.5}))
    ex = [t for t in st.trades_closed if t["symbol"] == "BTC"]
    idx = p["close"].index
    # entry fills at open of bar 210; day count 10 reached at close of bar 219; exit fills at open of 220
    assert ex and pd.Timestamp(ex[0]["exit_date"]) == idx[220]
