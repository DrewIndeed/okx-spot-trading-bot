"""Monte Carlo reproducibility and regime labels."""

from __future__ import annotations

import pandas as pd

from tradingbot.eval.v3_metrics import monte_carlo_trade_order, regime_labels


def test_monte_carlo_is_deterministic_with_seed():
    pnls = [0.05, -0.02, 0.01, -0.04, 0.03]
    a = monte_carlo_trade_order(pnls, capital=10000, n=200, seed=42)
    b = monte_carlo_trade_order(pnls, capital=10000, n=200, seed=42)
    c = monte_carlo_trade_order(pnls, capital=10000, n=200, seed=7)
    assert a["final_equity"]["p50"] == b["final_equity"]["p50"]
    assert a["maxDD"]["p50"] == b["maxDD"]["p50"]
    assert c["final_equity"]["p50"] != a["final_equity"]["p50"] or c["maxDD"]["p50"] != a["maxDD"]["p50"]


def test_regime_labels_bull_bear():
    idx = pd.date_range("2020-01-01", periods=400, freq="D")
    # Uptrend then crash.
    up = pd.Series(range(1, 301), index=idx[:300], dtype=float)
    down = pd.Series(list(range(300, 200, -1)), index=idx[300:], dtype=float)
    px = pd.concat([up, down])
    lab = regime_labels(px)
    # After SMA200 exists, early-high prices should not all be BEAR; the crash tail should be BEAR.
    assert "BEAR" in set(lab.dropna().iloc[-20:].tolist())
