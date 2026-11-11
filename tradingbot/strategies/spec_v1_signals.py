"""Precompute price-path signals for the three frozen variants. No OOS tuning."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.strategies import spec_v1_common as P
from tradingbot.universe import breadth_top20, monthly_top20_universe, sma


def universe_and_breadth(panel: dict) -> tuple[pd.DataFrame, pd.Series]:
    U = monthly_top20_universe(panel["close"], panel["vq"])
    br = breadth_top20(panel["close"], U, sma_n=P.VA_SMA_BREADTH)
    return U, br


def vol_ann(close: pd.DataFrame) -> pd.DataFrame:
    """Annualized σ from log close-to-close returns (research4.vol_ann convention).

    Spec says 'stdev of daily close-to-close returns, 90 sessions, min_periods=60,
    annualized × √365'. Log returns match the shop's research4.vol_ann; documented
    as an ambiguity resolution.
    """
    lr = np.log(close).diff()
    return lr.rolling(P.VOL_LOOKBACK, min_periods=P.VOL_MIN_PERIODS).std() * P.VOL_ANN_FACTOR


def simple_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change()


def cov_ann_at(rets: pd.DataFrame, t, names: list[str]) -> pd.DataFrame | None:
    """90-session annualized covariance of *log* returns ending at t (inclusive, closed)."""
    # Use log returns consistent with vol_ann.
    # rets here should already be log returns.
    window = rets.loc[:t, names].tail(P.VOL_LOOKBACK)
    window = window.dropna(how="all")
    if len(window) < P.VOL_MIN_PERIODS:
        return None
    cov = window.cov() * P.VOL_ANN_FACTOR ** 2
    return cov


def va_indicators(panel: dict, U: pd.DataFrame, breadth: pd.Series, r1_on: pd.Series) -> dict:
    C = panel["close"]
    prevmax = C.shift(1).rolling(P.VA_BREAKOUT_N, min_periods=P.VA_BREAKOUT_N).max()
    mid = (C.rolling(P.VA_STOP_N, min_periods=P.VA_STOP_N).max() + C.rolling(P.VA_STOP_N, min_periods=P.VA_STOP_N).min()) / 2.0
    r1 = r1_on.reindex(C.index).fillna(False)
    br_ok = breadth.reindex(C.index) >= P.BREADTH_MIN
    entry_flag = U & r1.values[:, None] & br_ok.values[:, None] & (C > prevmax) & (mid < C)
    return {"prevmax": prevmax, "mid": mid, "entry_flag": entry_flag.fillna(False)}


def _donchian_components(high: pd.Series, low: pd.Series, close: pd.Series, ns: tuple[int, ...]) -> dict[int, pd.DataFrame]:
    """Path-dependent Donchian long state per window n.

    Entry: close[t] > max(high[t-n:t-1]); exit: close[t] < mid_n, mid ratchets up only while long.
    mid_n = (max(high[t-n:t-1]) + min(low[t-n:t-1])) / 2.
    """
    out = {}
    c = close.values
    h = high.values
    l = low.values
    idx = close.index
    for n in ns:
        hh = high.shift(1).rolling(n, min_periods=n).max().values
        ll = low.shift(1).rolling(n, min_periods=n).min().values
        mid_raw = (hh + ll) / 2.0
        long = np.zeros(len(c), dtype=bool)
        stop = np.full(len(c), np.nan)
        pos = False
        st = np.nan
        for t in range(len(c)):
            if np.isnan(c[t]) or np.isnan(hh[t]):
                if pos:
                    # missing bar: keep state, no update
                    long[t] = True
                    stop[t] = st
                continue
            mid = mid_raw[t]
            if pos:
                if not np.isnan(mid):
                    st = mid if np.isnan(st) else max(st, mid)
                if c[t] < st:
                    pos = False
                    st = np.nan
                    long[t] = False
                    stop[t] = np.nan
                else:
                    long[t] = True
                    stop[t] = st
            else:
                if c[t] > hh[t]:
                    pos = True
                    st = mid
                    long[t] = True
                    stop[t] = st
        out[n] = pd.DataFrame({"long": long, "stop": stop, "mid_raw": mid_raw}, index=idx)
    return out


def _tsmom_components(close: pd.Series, ks: tuple[int, ...]) -> dict[int, pd.Series]:
    out = {}
    for k in ks:
        out[k] = (close / close.shift(k) > 1.0).fillna(False)
    return out


def vb_signals(panel: dict, U: pd.DataFrame) -> dict:
    C, HI, LO = panel["close"], panel["high"], panel["low"]
    idx, cols = C.index, C.columns
    tsmom = {k: pd.DataFrame(False, index=idx, columns=cols) for k in P.VB_TSMOM}
    donch_long = {n: pd.DataFrame(False, index=idx, columns=cols) for n in P.VB_DONCHIAN}
    donch_stop = {n: pd.DataFrame(np.nan, index=idx, columns=cols) for n in P.VB_DONCHIAN}
    for s in cols:
        tm = _tsmom_components(C[s], P.VB_TSMOM)
        for k, ser in tm.items():
            tsmom[k][s] = ser
        dc = _donchian_components(HI[s], LO[s], C[s], P.VB_DONCHIAN)
        for n, df in dc.items():
            donch_long[n][s] = df["long"]
            donch_stop[n][s] = df["stop"]
    # count components
    count = pd.DataFrame(0, index=idx, columns=cols, dtype=int)
    for k in P.VB_TSMOM:
        count = count + tsmom[k].astype(int)
    for n in P.VB_DONCHIAN:
        count = count + donch_long[n].astype(int)
    combined = (count >= P.VB_MIN_COMPONENTS) & U
    # tightest (highest) mid-stop among Donchian components currently long
    layers = [donch_stop[n].where(donch_long[n]).values for n in P.VB_DONCHIAN]
    stacked = np.stack(layers, axis=0)
    tight_v = stacked[0].copy()
    for layer in stacked[1:]:
        tight_v = np.fmax(tight_v, layer)
    tight = pd.DataFrame(tight_v, index=idx, columns=cols)
    # If no Donchian component is long, combined cannot be ≥4 (max TSMOM is 3) — still guard.
    no_donch = ~np.any(np.stack([donch_long[n].values for n in P.VB_DONCHIAN], axis=0), axis=0)
    combined = combined & ~pd.DataFrame(no_donch, index=idx, columns=cols)
    return {
        "count": count,
        "combined": combined.fillna(False),
        "stop": tight,
        "tsmom": tsmom,
        "donch_long": donch_long,
        "donch_stop": donch_stop,
    }


def vc_signals(panel: dict, U: pd.DataFrame) -> dict:
    C, HI, LO = panel["close"], panel["high"], panel["low"]
    idx, cols = C.index, C.columns
    sleeve_long = {n: pd.DataFrame(False, index=idx, columns=cols) for n in P.VC_DONCHIAN}
    sleeve_stop = {n: pd.DataFrame(np.nan, index=idx, columns=cols) for n in P.VC_DONCHIAN}
    for s in cols:
        dc = _donchian_components(HI[s], LO[s], C[s], P.VC_DONCHIAN)
        for n, df in dc.items():
            sleeve_long[n][s] = df["long"] & U[s]
            sleeve_stop[n][s] = df["stop"]
    any_long = sleeve_long[P.VC_DONCHIAN[0]].copy()
    for n in P.VC_DONCHIAN[1:]:
        any_long = any_long | sleeve_long[n]
    return {"sleeve_long": sleeve_long, "sleeve_stop": sleeve_stop, "any_long": any_long.fillna(False)}
