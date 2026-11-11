import numpy as np
import pandas as pd
import pytest

from tradingbot.strategy.d1_breakout import D1BreakoutStrategy


def _panel():
    # 250 days, 3 coins. BTC trends up through SMA200. ALT breakouts on last day.
    idx = pd.date_range("2024-01-01", periods=250, freq="D")
    btc = pd.Series(np.linspace(20, 40, 250), index=idx)
    # ALT: flat 100 for a long time, then a 20-day high close on the last bar
    alt = pd.Series(100.0, index=idx)
    alt.iloc[-1] = 130.0
    # another alt that does not break out
    flat = pd.Series(80.0, index=idx)
    C = pd.DataFrame({"BTC": btc, "ALT": alt, "FLAT": flat})
    # quote vol: all eligible
    VQ = pd.DataFrame(3e6, index=idx, columns=C.columns)
    return C, VQ


def test_d1_breakout_entry_ratchet_and_size_cap():
    C, VQ = _panel()
    s = D1BreakoutStrategy(allocated_capital=10_000)
    s.prepare(C, quote_vol=VQ)
    state = {}
    w, trades = s.weights_and_trades(state=state)
    # ALT should be in state as a new entry on last bar
    assert "ALT" in state
    st = state["ALT"]
    assert st["new"] is True
    assert st["entry"] == pytest.approx(130.0)
    # mid-stop of last 20 closes: 19*100 + 130 = 2030/20? rolling 20 includes the 130 close
    # mid = (max20 + min20)/2 = (130+100)/2 = 115, and 115 < 130
    assert st["stop"] == pytest.approx(115.0)
    risk = (130 - 115) / 130
    assert st["risk"] == pytest.approx(risk)
    # size = min(0.01/risk, 0.10)
    assert st["size"] == pytest.approx(min(0.01 / risk, 0.10))
    assert st["size"] <= 0.10 + 1e-12
    # FLAT should not enter
    assert "FLAT" not in state
    # last weight on ALT equals size (only one position, no cap_total scale)
    assert w["ALT"].iloc[-1] == pytest.approx(st["size"])


def test_d1_requires_allocated_capital():
    with pytest.raises(ValueError, match="allocated_capital"):
        D1BreakoutStrategy(allocated_capital=0)
