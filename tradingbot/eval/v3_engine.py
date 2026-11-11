"""Parameterized paper/backtest path for spec v3.

Reuses v2 fill/mark/book helpers (imported, not edited). Paper only. C_paper required.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from tradingbot.eval.v2_engine import (
    BookState,
    _fill_to_weights,
    _mark,
    curve_series,
    new_book,
)
from tradingbot.strategies import spec_v2_common as P
from tradingbot.strategies.spec_v1_signals import cov_ann_at, vol_ann

TargetFn = Callable[[pd.Timestamp, dict, dict, pd.Series, str], pd.Series]


def remap_regime(regime: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Apply a pre-registered regime lever. BTC stale HALT is never removed."""
    out = regime.copy()
    f2 = out["f2_halt"].astype(bool)
    f1 = out["f1_reduce"].astype(bool)
    stale = out["btc_stale_halt"].astype(bool)
    state = pd.Series("NORMAL", index=out.index)
    if mode == "as_is":
        state = out["macroy_state"]
    elif mode == "no_f2":
        state = state.mask(f1, "REDUCE")
        state = state.mask(stale, "HALT")
    elif mode == "no_f1":
        state = state.mask(f2 | stale, "HALT")
    elif mode == "none":
        state = state.mask(stale, "HALT")
    elif mode == "f2_reduce":
        state = state.mask(f1, "REDUCE")
        state = state.mask(f2 & ~stale, "REDUCE")
        state = state.mask(stale, "HALT")
    else:
        raise ValueError(f"unknown regime mode {mode!r}")
    out["macroy_state"] = state
    out["macroy_mult"] = state.map({"NORMAL": 1.0, "REDUCE": 0.5, "HALT": 0.0}).astype(float)
    return out


def apply_regime_to_weights(w: pd.Series, current_w: pd.Series, regime_state: str) -> pd.Series:
    cols = list(w.index)
    cur = current_w.reindex(cols).astype(float).fillna(0.0)
    w = w.reindex(cols).astype(float).fillna(0.0)
    if regime_state == "HALT":
        return pd.Series(np.minimum(w.values, cur.values), index=cols)
    if regime_state == "REDUCE":
        inc = np.maximum(w.values - cur.values, 0.0)
        held = np.minimum(w.values, cur.values)
        return pd.Series(held + 0.5 * inc, index=cols)
    return w



def clip_caps(w: pd.Series, name_cap: float | None, gross_cap: float = P.CORE_GROSS_CAP) -> pd.Series:
    w = w.astype(float).clip(lower=0.0)
    if name_cap is not None:
        w = w.clip(upper=float(name_cap))
    s = float(w.sum())
    if s > gross_cap and s > 0:
        w = w * (gross_cap / s)
        if name_cap is not None:
            w = w.clip(upper=float(name_cap))
            s = float(w.sum())
            if s > gross_cap and s > 0:
                w = w * (gross_cap / s)
    return w


def apply_increase_block(desired: pd.Series, current_w: pd.Series, blocked: set[str]) -> pd.Series:
    out = desired.astype(float).copy()
    cur = current_w.reindex(out.index).astype(float).fillna(0.0)
    for s in out.index:
        if s in blocked:
            out[s] = min(float(out[s]), float(cur[s]))
    return out


def apply_vol_band(target: dict, qty: dict, open_px: dict, equity: float, threshold: float = 0.20) -> dict:
    """Skip tiny rebalances for names that stay long. Signal exits (target 0) always trade."""
    if equity <= 0:
        return target
    out = dict(target)
    for s, q in qty.items():
        ox = open_px.get(s)
        if ox is None or pd.isna(ox) or float(ox) <= 0:
            continue
        w_cur = float(q) * float(ox) / equity
        w_tgt = float(out.get(s, 0.0))
        if w_cur > 1e-12 and w_tgt > 1e-12:
            denom = max(w_cur, 0.01)
            if abs(w_tgt - w_cur) / denom < threshold:
                out[s] = w_cur
    return out


def vol_target_weights(
    t,
    pre: dict,
    long_mask_row: pd.Series,
    current_w: pd.Series,
    regime_state: str,
    *,
    vol_target: float = P.CORE_VOL_TARGET,
    gross_cap: float = P.CORE_GROSS_CAP,
    symbols: list[str] | None = None,
) -> pd.Series:
    cols = list(symbols or long_mask_row.index)
    w = pd.Series(0.0, index=cols)
    vol = pre["vol"].loc[t] if t in pre["vol"].index else pd.Series(dtype=float)
    for s in cols:
        if not bool(long_mask_row.get(s, False)):
            continue
        sig_vol = vol.get(s, np.nan) if hasattr(vol, "get") else np.nan
        if pd.isna(sig_vol) or float(sig_vol) <= 0:
            continue
        w[s] = vol_target / float(sig_vol)
    w = w.clip(lower=0.0)
    active = [s for s in cols if float(w[s]) > 0]
    if active:
        cov = cov_ann_at(pre["log_rets"], t, active)
        if cov is not None:
            cov = cov.reindex(index=active, columns=active).fillna(0.0)
            wv = w[active].astype(float).values
            pred = float(np.sqrt(max(0.0, wv @ cov.values @ wv)))
            if pred > vol_target and pred > 0:
                w[active] = w[active] * (vol_target / pred)
    ssum = float(w.sum())
    if ssum > gross_cap and ssum > 0:
        w = w * (gross_cap / ssum)
    return apply_regime_to_weights(w, current_w, regime_state)


def equal_weights(
    long_mask_row: pd.Series,
    current_w: pd.Series,
    regime_state: str,
    *,
    gross_cap: float = P.CORE_GROSS_CAP,
    symbols: list[str] | None = None,
) -> pd.Series:
    cols = list(symbols or long_mask_row.index)
    active = [s for s in cols if bool(long_mask_row.get(s, False))]
    w = pd.Series(0.0, index=cols)
    if active:
        w[active] = gross_cap / len(active)
    return apply_regime_to_weights(w, current_w, regime_state)


def scale_to_vol(
    t,
    pre: dict,
    raw: pd.Series,
    current_w: pd.Series,
    regime_state: str,
    *,
    vol_target: float = P.CORE_VOL_TARGET,
    gross_cap: float = P.CORE_GROSS_CAP,
) -> pd.Series:
    w = raw.astype(float).clip(lower=0.0)
    cols = list(w.index)
    active = [s for s in cols if float(w[s]) > 0]
    if active:
        cov = cov_ann_at(pre["log_rets"], t, active)
        if cov is not None:
            cov = cov.reindex(index=active, columns=active).fillna(0.0)
            wv = w[active].astype(float).values
            pred = float(np.sqrt(max(0.0, wv @ cov.values @ wv)))
            if pred > vol_target and pred > 0:
                w[active] = w[active] * (vol_target / pred)
    ssum = float(w.sum())
    if ssum > gross_cap and ssum > 0:
        w = w * (gross_cap / ssum)
    return apply_regime_to_weights(w, current_w, regime_state)


def step_generic(
    st: BookState,
    date: pd.Timestamp,
    panel: dict,
    pre: dict,
    regime_row: pd.Series,
    target_fn: TargetFn,
    symbols: list[str],
    *,
    cost: float = P.COST_2X,
    use_band: bool = False,
    band_membership_only: bool = False,
) -> None:
    opens, close = panel["open"], panel["close"]
    o = {
        s: float(opens.at[date, s]) if s in opens.columns and pd.notna(opens.at[date, s]) else np.nan
        for s in symbols
        if s in opens.columns
    }
    c = {
        s: float(close.at[date, s]) if s in close.columns and pd.notna(close.at[date, s]) else np.nan
        for s in symbols
        if s in close.columns
    }

    flatten = bool(st.kill_pending or (st.killed and sum(abs(q) for q in st.qty.values()) > 0))
    if st.kill_pending:
        st.killed = True
        if st.killed_on is None:
            st.killed_on = str(pd.Timestamp(date).date())
        st.kill_pending = False
    block = st.block_new
    st.block_new = False
    target = dict(st.pending) if st.pending else {s: 0.0 for s in st.qty}
    if use_band and not flatten and not st.killed and not st.kill_pending:
        apply = True
        if band_membership_only:
            cur_names = {s for s, q in st.qty.items() if abs(float(q)) > 1e-12}
            tgt_names = {s for s, w in target.items() if float(w) > 1e-12}
            apply = cur_names == tgt_names
        if apply:
            target = apply_vol_band(target, st.qty, o, float(st.equity))
    traded = _fill_to_weights(st, target, o, date, cost, block_new=block, flatten=flatten)
    r = _mark(st, c, date, traded)

    if st.equity > st.peak:
        st.peak = st.equity
    dd = (st.equity / st.peak - 1.0) if st.peak > 0 else 0.0
    if (not st.killed) and dd <= P.CORE_KILL_DD:
        st.kill_pending = True
    if r <= P.CORE_DAILY_LOSS_CAP:
        st.block_new = True

    held = sum(abs(float(q)) for q in st.qty.values()) > 1e-12
    if st.killed:
        if held:
            st.flat_days = 0
        else:
            st.flat_days += 1
            btc_px = c.get("BTC")
            if btc_px is None or pd.isna(btc_px):
                btc_px = float(close.at[date, "BTC"]) if "BTC" in close.columns and pd.notna(close.at[date, "BTC"]) else np.nan
            s200 = np.nan
            if "sma200_btc" in pre and date in pre["sma200_btc"].index:
                s200 = pre["sma200_btc"].at[date]
            elif "sma200" in pre and "BTC" in pre["sma200"].columns and date in pre["sma200"].index:
                s200 = pre["sma200"]["BTC"].at[date]
            if (
                st.flat_days >= P.CORE_REARM_FLAT_DAYS
                and pd.notna(btc_px)
                and pd.notna(s200)
                and float(btc_px) > float(s200)
            ):
                st.killed = False
                st.kill_pending = False
                st.flat_days = 0
                st.peak = st.equity
                st.rearmed_on.append(str(pd.Timestamp(date).date()))

    if st.killed or st.kill_pending:
        st.pending = {s: 0.0 for s in st.qty}
    else:
        state = str(regime_row.get("macroy_state", "NORMAL"))
        cur_w = pd.Series(
            {
                s: (float(st.qty.get(s, 0.0)) * float(c[s]) / st.equity)
                if st.equity and s in c and pd.notna(c.get(s))
                else 0.0
                for s in symbols
            }
        )
        w = target_fn(date, panel, pre, cur_w, state)
        st.pending = {s: float(w.get(s, 0.0)) for s in (w.index if hasattr(w, "index") else symbols)}
        st.pending_signal_date = str(pd.Timestamp(date).date())
    st.last_processed = str(pd.Timestamp(date).date())


def run_generic_path(
    name: str,
    symbols: list[str],
    target_fn: TargetFn,
    panel: dict,
    pre: dict,
    regime: pd.DataFrame,
    capital: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    cost: float = P.COST_2X,
    use_band: bool = False,
    band_membership_only: bool = False,
) -> BookState:
    st = new_book(name, capital, list(symbols))
    dates = panel["close"].index[(panel["close"].index >= start) & (panel["close"].index <= end)]
    pre_dates = panel["close"].index[panel["close"].index < start]
    if len(pre_dates):
        t0 = pre_dates[-1]
        row = regime.loc[t0] if t0 in regime.index else pd.Series({"macroy_state": "NORMAL"})
        state = str(row.get("macroy_state", "NORMAL")) if "macroy_state" in getattr(row, "index", []) else "NORMAL"
        cur = pd.Series(0.0, index=list(symbols))
        w = target_fn(t0, panel, pre, cur, state)
        st.pending = {s: float(w.get(s, 0.0)) for s in (w.index if hasattr(w, "index") else symbols)}
        st.pending_signal_date = str(pd.Timestamp(t0).date())
    for dt in dates:
        row = regime.loc[dt] if dt in regime.index else pd.Series({"macroy_state": "NORMAL"})
        step_generic(st, dt, panel, pre, row, target_fn, symbols, cost=cost, use_band=use_band, band_membership_only=band_membership_only)
    return st


def common_pre(panel: dict, symbols: list[str] | None = None) -> dict:
    cols = [c for c in (symbols or list(panel["close"].columns)) if c in panel["close"].columns]
    C = panel["close"][cols]
    from tradingbot.universe import sma

    return {
        "vol": vol_ann(C),
        "log_rets": np.log(C).diff(),
        "sma200": sma(C, 200),
        "sma200_btc": sma(panel["close"]["BTC"], 200) if "BTC" in panel["close"].columns else pd.Series(dtype=float),
        "sma100": sma(C, 100),
        "ret90": C / C.shift(90) - 1.0,
    }
