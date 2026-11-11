"""F1 / F2 / R1 regime state for spec v1. Worst state wins."""

from __future__ import annotations

import pandas as pd

from tradingbot.eval.events import f2_halt_mask
from tradingbot.strategies.spec_v1_common import F1_BTC_STALE_DAYS, F1_SMA
from tradingbot.universe import sma

NORMAL, REDUCE, HALT = "NORMAL", "REDUCE", "HALT"
MULT = {NORMAL: 1.0, REDUCE: 0.5, HALT: 0.0}
_RANK = {NORMAL: 0, REDUCE: 1, HALT: 2}


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def btc_sma200(close_btc: pd.Series) -> pd.Series:
    return sma(close_btc, F1_SMA)


def f1_reduce_and_stale_halt(close_btc: pd.Series) -> tuple[pd.Series, pd.Series]:
    """F1 REDUCE when last closed BTC close <= SMA200.

    Missing BTC closed bar for 2 calendar days → HALT (separate series).
    SMA uses 200 closed closes, no open bar (caller must pass confirm==1 only).
    """
    s200 = btc_sma200(close_btc)
    reduce = (close_btc <= s200) & s200.notna()
    # stale: calendar gap > F1_BTC_STALE_DAYS since last non-NA close
    stale = pd.Series(False, index=close_btc.index)
    last_good: pd.Timestamp | None = None
    for dt, px in close_btc.items():
        if pd.notna(px):
            last_good = pd.Timestamp(dt)
            stale.loc[dt] = False
        else:
            if last_good is None:
                stale.loc[dt] = True
            else:
                stale.loc[dt] = (pd.Timestamp(dt) - last_good).days >= F1_BTC_STALE_DAYS
    return reduce.fillna(False), stale


def build_regime(panel: dict, *, now=None) -> pd.DataFrame:
    """Return a DataFrame with F2 halt, F1 reduce, BTC stale halt, R1 on, and combined states.

    Combined Macroy state (F2 then F1; no F3/F4):
      - f2_halt or btc_stale → HALT
      - else f1_reduce → REDUCE
      - else NORMAL
    R1 (V-A production) is a separate hard gate: btc_close > SMA200 (and SMA exists).
    """
    close = panel["close"]
    btc = close["BTC"]
    idx = close.index
    f2 = f2_halt_mask(idx, now=now)
    f1_red, btc_stale = f1_reduce_and_stale_halt(btc)
    s200 = btc_sma200(btc)
    r1_on = (btc > s200) & s200.notna()
    state = pd.Series(NORMAL, index=idx)
    state = state.mask(f1_red, REDUCE)
    state = state.mask(f2 | btc_stale, HALT)
    return pd.DataFrame(
        {
            "f2_halt": f2.astype(bool),
            "f1_reduce": f1_red.astype(bool),
            "btc_stale_halt": btc_stale.astype(bool),
            "r1_on": r1_on.fillna(False).astype(bool),
            "macroy_state": state,
            "macroy_mult": state.map(MULT).astype(float),
        },
        index=idx,
    )
