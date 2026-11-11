"""V2-CORE signals: BTC+ETH long-flat, close>SMA200 and >=2/3 Donchian 20/60/90.

Donchian definition is imported from spec v1 (unchanged): entry close[t] >
max(high[t-n:t-1]); exit close[t] < ratcheted mid. This file does not modify v1 sources.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.strategies import spec_v2_common as P
from tradingbot.strategies.spec_v1_signals import _donchian_components, cov_ann_at, vol_ann
from tradingbot.universe import sma


def core_signals(panel: dict) -> dict:
    """Return long mask, component count, and per-window Donchian state for BTC and ETH."""
    C, HI, LO = panel["close"], panel["high"], panel["low"]
    cols = [c for c in P.CORE_UNIVERSE if c in C.columns]
    idx = C.index
    long_mask = pd.DataFrame(False, index=idx, columns=cols)
    count = pd.DataFrame(0, index=idx, columns=cols, dtype=int)
    donch_long = {n: pd.DataFrame(False, index=idx, columns=cols) for n in P.CORE_DONCHIAN}
    donch_stop = {n: pd.DataFrame(np.nan, index=idx, columns=cols) for n in P.CORE_DONCHIAN}
    s200 = sma(C[cols], P.CORE_SMA)
    above = C[cols] > s200
    for s in cols:
        dc = _donchian_components(HI[s], LO[s], C[s], P.CORE_DONCHIAN)
        n_on = pd.Series(0, index=idx, dtype=int)
        for n, df in dc.items():
            donch_long[n][s] = df["long"]
            donch_stop[n][s] = df["stop"]
            n_on = n_on + df["long"].astype(int)
        count[s] = n_on
        long_mask[s] = above[s].fillna(False) & (n_on >= P.CORE_MIN_DONCHIAN)
    return {
        "long": long_mask.fillna(False),
        "count": count,
        "above_sma200": above.fillna(False),
        "sma200": s200,
        "donch_long": donch_long,
        "donch_stop": donch_stop,
        "vol": vol_ann(C[cols]),
        "log_rets": np.log(C[cols]).diff(),
    }


def core_target_weights(t, panel: dict, pre: dict, current_w: pd.Series, regime_state: str) -> pd.Series:
    """Vol-target 12% weights for names that are long at close t, then regime on new risk.

    Gross cap 100%, no leverage. No v1 10% name cap (not in spec v2).
    """
    cols = [c for c in P.CORE_UNIVERSE if c in panel["close"].columns]
    w = pd.Series(0.0, index=cols)
    if regime_state == "HALT":
        # no new risk / no increases; exits still happen via signal=0 below
        pass
    sig = pre["long"].loc[t]
    vol = pre["vol"].loc[t]
    for s in cols:
        if not bool(sig.get(s, False)):
            continue
        sig_vol = vol.get(s, np.nan)
        if pd.isna(sig_vol) or float(sig_vol) <= 0:
            continue
        w[s] = P.CORE_VOL_TARGET / float(sig_vol)
    w = w.clip(lower=0.0)
    # Scale so forecast portfolio vol <= 12%
    active = [s for s in cols if float(w[s]) > 0]
    if active:
        cov = cov_ann_at(pre["log_rets"], t, active)
        if cov is not None:
            cov = cov.reindex(index=active, columns=active).fillna(0.0)
            wv = w[active].astype(float).values
            pred = float(np.sqrt(max(0.0, wv @ cov.values @ wv)))
            if pred > P.CORE_VOL_TARGET and pred > 0:
                w[active] = w[active] * (P.CORE_VOL_TARGET / pred)
    ssum = float(w.sum())
    if ssum > P.CORE_GROSS_CAP and ssum > 0:
        w = w * (P.CORE_GROSS_CAP / ssum)
    # Regime: HALT = no increases; REDUCE = 0.5 on new risk
    cur = current_w.reindex(cols).astype(float).fillna(0.0)
    if regime_state == "HALT":
        w = pd.Series(np.minimum(w.values, cur.values), index=cols)
    elif regime_state == "REDUCE":
        inc = np.maximum(w.values - cur.values, 0.0)
        held = np.minimum(w.values, cur.values)
        w = pd.Series(held + 0.5 * inc, index=cols)
    return w
