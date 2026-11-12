"""v4 SPOT-only exit/entry overlays on top of a frozen v3 target function.

"Sell" here means exiting or trimming an existing long (trailing ATR stop, time exit,
partial scale-out, volatility-shock trim). No shorting, margin or derivatives.
Signals use data up to the close of bar t only; the engine fills them at the open of t+1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from tradingbot.eval import v3_engine as V3

_ORIG_FILL = V3._fill_to_weights


@dataclass(frozen=True)
class V4Config:
    trail_atr: float | None = None   # exit if close < highest close since entry - k*ATR
    scale_atr: float | None = None   # trim to scale_frac once close >= entry + k*ATR(entry)
    scale_frac: float = 0.5
    time_days: int | None = None     # exit if held >= N sessions and close <= entry price
    buy_trend: bool = False          # new entries only if BTC > SMA100 and name > SMA50
    vol_shock: float | None = None   # BTC sigma10 > k * trailing 365d median -> halve, block increases
    atr_n: int = 14


def atr(panel: dict, n: int = 14) -> pd.DataFrame:
    H, L, C = panel["high"], panel["low"], panel["close"]
    pc = C.shift(1)
    tr = np.maximum(H - L, np.maximum((H - pc).abs(), (L - pc).abs()))
    return tr.rolling(n, min_periods=n).mean()


def btc_vol_shock(close_btc: pd.Series, k: float) -> pd.Series:
    lr = np.log(close_btc).diff()
    s10 = lr.rolling(10, min_periods=10).std()
    med = s10.shift(1).rolling(365, min_periods=180).median()
    return ((s10 > k * med) & med.notna()).fillna(False)


def make_v4_target(base_fn: Callable, cfg: V4Config, panel: dict) -> Callable:
    """Wrap base_fn. Fresh state per call; build a new one for every backtest run."""
    C, O = panel["close"], panel["open"]
    A = atr(panel, cfg.atr_n)
    sma50 = C.rolling(50, min_periods=50).mean()
    btc_sma100 = C["BTC"].rolling(100, min_periods=100).mean() if "BTC" in C.columns else None
    shock = btc_vol_shock(C["BTC"], cfg.vol_shock) if (cfg.vol_shock and "BTC" in C.columns) else None
    pos: dict[str, dict] = {}
    lock: set[str] = set()

    def _f(df, t, s):
        try:
            v = df.at[t, s]
        except KeyError:
            return np.nan
        return float(v) if pd.notna(v) else np.nan

    def fn(t, panel_, pre, cur, state):
        desired = base_fn(t, panel_, pre, cur, state).astype(float).copy()
        cur = cur.reindex(desired.index).astype(float).fillna(0.0)
        for s in desired.index:
            held = cur[s] > 1e-9
            if s in lock and desired[s] <= 1e-12:
                lock.discard(s)
            if not held:
                pos.pop(s, None)
                if s in lock:
                    desired[s] = 0.0
                elif cfg.buy_trend and desired[s] > 0:
                    b, bs = _f(C, t, "BTC"), (float(btc_sma100.get(t, np.nan)) if btc_sma100 is not None else np.nan)
                    c, m = _f(C, t, s), _f(sma50, t, s)
                    if not (b > bs and c > m):
                        desired[s] = 0.0
                continue
            c = _f(C, t, s)
            if np.isnan(c):
                continue
            p = pos.get(s)
            if p is None:
                e = _f(O, t, s)
                p = pos[s] = {"entry": c if np.isnan(e) else e, "hi": c, "atr_e": _f(A, t, s), "days": 0, "scaled": False}
            p["hi"] = max(p["hi"], c)
            p["days"] += 1
            a = _f(A, t, s)
            out = bool(cfg.trail_atr and np.isfinite(a) and c < p["hi"] - cfg.trail_atr * a)
            out = out or bool(cfg.time_days and p["days"] >= cfg.time_days and c <= p["entry"])
            if out:
                desired[s] = 0.0
                lock.add(s)
                continue
            if cfg.scale_atr and np.isfinite(p["atr_e"]) and c >= p["entry"] + cfg.scale_atr * p["atr_e"]:
                p["scaled"] = True
            if p["scaled"]:
                desired[s] = desired[s] * cfg.scale_frac
        if shock is not None and bool(shock.get(t, False)):
            desired = pd.Series(np.minimum(0.5 * desired.values, cur.values), index=desired.index)
        return desired

    return fn


def fill_exits_then_entries(st, target_w, open_px, date, cost, *, block_new, flatten, _probe=None) -> float:
    """Same fills as the v2 engine, but all reductions are executed before any increase."""
    eq = float(st.equity)
    if flatten or eq <= 0:
        return _ORIG_FILL(st, target_w, open_px, date, cost, block_new=block_new, flatten=flatten)
    exits = {}
    for s, q in st.qty.items():
        ox = open_px.get(s)
        tgt = float(target_w.get(s, 0.0))
        if ox is None or pd.isna(ox) or float(ox) <= 0:
            exits[s] = tgt
        else:
            exits[s] = min(tgt, float(q) * float(ox) / eq)
    t1 = _ORIG_FILL(st, exits, open_px, date, cost, block_new=False, flatten=False)
    if _probe is not None:
        _probe(st)
    t2 = _ORIG_FILL(st, target_w, open_px, date, cost, block_new=block_new, flatten=False)
    return t1 + t2


def run_v4_path(*args, **kwargs):
    """v3 run_generic_path with exit-before-entry fills (v3 module is not edited; swapped only for this call)."""
    V3._fill_to_weights = fill_exits_then_entries
    try:
        return V3.run_generic_path(*args, **kwargs)
    finally:
        V3._fill_to_weights = _ORIG_FILL
