"""Regime helpers. Two D1 gates (data contract §4) plus a combined entry helper.

- BTC close > SMA200  (D1 production flatten / allow-on)
- breadth >= 50% of point-in-time top-20 with close > SMA50  (no new D1 entries if false)
Combined helper is AND of the two (typical 'allow new long' check).

This is not the 4H setup filter in /workspace/crypto/regime.py (that one is BTC close > EMA200).
We copy the SMA rule rather than importing regime.py (which can pull CoinGecko via matrix()).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from tradingbot.universe import sma


@dataclass(frozen=True)
class RegimeState:
    btc_above_sma200: bool
    breadth: float
    breadth_ok: bool
    allow_new_entries: bool
    allow_hold: bool  # BTC>SMA200; if false, D1 flattens alts
    btc_close: Optional[float] = None
    btc_sma200: Optional[float] = None
    asof: object | None = None


def btc_above_sma200(btc_close: pd.Series, n: int = 200) -> pd.Series:
    s = sma(btc_close, n)
    return btc_close > s


def d1_entry_regime(btc_close: pd.Series, breadth: pd.Series) -> pd.Series:
    """True when BTC close > SMA200 AND breadth >= 0.5. Vector of booleans, aligned to close index."""
    gate = btc_above_sma200(btc_close).reindex(breadth.index)
    return (gate.fillna(False) & (breadth >= 0.5)).fillna(False)


def regime_at(btc_close: pd.Series, breadth: pd.Series, ts=None) -> RegimeState:
    ts = ts if ts is not None else btc_close.index[-1]
    s200 = sma(btc_close, 200)
    c = float(btc_close.loc[ts]) if ts in btc_close.index and pd.notna(btc_close.loc[ts]) else None
    s = float(s200.loc[ts]) if ts in s200.index and pd.notna(s200.loc[ts]) else None
    br = float(breadth.loc[ts]) if ts in breadth.index and pd.notna(breadth.loc[ts]) else float("nan")
    above = bool(c is not None and s is not None and c > s)
    br_ok = bool(br >= 0.5)
    return RegimeState(
        btc_above_sma200=above,
        breadth=br,
        breadth_ok=br_ok,
        allow_new_entries=above and br_ok,
        allow_hold=above,
        btc_close=c,
        btc_sma200=s,
        asof=ts,
    )
