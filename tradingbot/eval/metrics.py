"""Metrics for strategy-spec v1 (same Sharpe / DSR definitions as research4)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Iterable, Optional

import numpy as np
import pandas as pd

ANN = math.sqrt(365.0)


def sharpe_daily_series(r: pd.Series) -> float:
    """Annualized Sharpe: mean(r)/std(r)*sqrt(365). Cash days included. 0 if std==0."""
    x = r.dropna().astype(float)
    if len(x) < 2:
        return 0.0
    sd = float(x.std(ddof=1)) if len(x) > 1 else 0.0
    if sd == 0.0 or np.isnan(sd):
        return 0.0
    return float(x.mean() / sd * ANN)


def daily_sharpe_raw(r: pd.Series) -> float:
    """Un-annualized daily Sharpe (DSR input, research4 units)."""
    x = r.dropna().astype(float)
    if len(x) < 2:
        return 0.0
    sd = float(x.std(ddof=1))
    if sd == 0.0 or np.isnan(sd):
        return 0.0
    return float(x.mean() / sd)


def max_drawdown(equity: pd.Series) -> float:
    """Peak-to-trough of the equity curve as a negative fraction (e.g. -0.15)."""
    eq = equity.dropna().astype(float)
    if eq.empty:
        return 0.0
    return float((eq / eq.cummax() - 1.0).min())


def cagr(equity: pd.Series) -> float:
    eq = equity.dropna().astype(float)
    if len(eq) < 2 or eq.iloc[0] <= 0 or eq.iloc[-1] <= 0:
        return 0.0
    years = len(eq) / 365.0
    if years <= 0:
        return 0.0
    return float(eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0


def dsr(sr_d: float, T: int, skew: float, kurt: float, sr_trials_var: float, N: int) -> tuple[float, float]:
    """Deflated Sharpe (Bailey & López de Prado 2014), copied from research4.dsr.

    ``sr_d`` is the *daily* Sharpe (mean/std, not annualized).
    ``kurt`` is raw kurtosis (pandas excess kurtosis + 3).
    Returns (DSR probability, implied SR0).
    """
    nd = NormalDist()
    em = 0.5772156649
    n = max(int(N), 1)
    sr0 = math.sqrt(max(0.0, sr_trials_var)) * (
        (1 - em) * nd.inv_cdf(1 - 1 / n) + em * nd.inv_cdf(1 - 1 / (n * math.e))
    )
    den = math.sqrt(max(1e-12, 1 - skew * sr_d + (kurt - 1) / 4 * sr_d**2))
    if T <= 1:
        return 0.0, sr0
    return nd.cdf((sr_d - sr0) * math.sqrt(T - 1) / den), sr0


def dsr_from_returns(r: pd.Series, sr_trials_var: float, N: int) -> dict:
    x = r.dropna().astype(float)
    sr_d = daily_sharpe_raw(x)
    T = int(len(x))
    skew = float(x.skew()) if T > 2 else 0.0
    # pandas Series.kurt() is excess kurtosis; research4 adds 3 to get raw.
    kurt = float(x.kurt() + 3.0) if T > 3 else 3.0
    val, sr0 = dsr(sr_d, T, skew, kurt, sr_trials_var, N)
    return {
        "DSR": float(val),
        "SR0": float(sr0),
        "sr_daily": float(sr_d),
        "T": T,
        "skew": skew,
        "kurt": kurt,
        "N": int(N),
        "sr_trials_var": float(sr_trials_var),
    }


@dataclass
class Trade:
    variant: str
    symbol: str
    entry_date: pd.Timestamp  # fill date (open of t+1)
    exit_date: pd.Timestamp | None
    entry_px: float
    exit_px: float | None
    stop_at_entry: float | None
    cost_per_side: float
    pnl_frac: float | None = None  # return on allocated name notional, net of costs
    r_multiple: float | None = None
    reason: str = ""


def trade_stats(trades: Iterable[Trade]) -> dict:
    closed = [t for t in trades if t.exit_date is not None and t.pnl_frac is not None]
    n = len(closed)
    if n == 0:
        return {
            "n": 0,
            "n_opened": sum(1 for t in trades if t.entry_date is not None),
            "n_closed": 0,
            "win_rate": None,
            "PF": None,
            "expectancy_R": None,
            "avg_trade_pct": None,
        }
    pnls = [float(t.pnl_frac) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pos, neg = sum(wins), -sum(losses)
    rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
    return {
        "n": n,
        "n_opened": sum(1 for t in trades if t.entry_date is not None),
        "n_closed": n,
        "win_rate": float(np.mean([p > 0 for p in pnls])),
        "PF": (pos / neg) if neg > 0 else (math.inf if pos > 0 else None),
        "expectancy_R": float(np.mean(rs)) if rs else None,
        "avg_trade_pct": float(np.mean(pnls)),
        "n_with_R": len(rs),
    }


def window_metrics(
    equity: pd.Series,
    rets: pd.Series,
    trades: list[Trade],
    start: pd.Timestamp,
    end: pd.Timestamp,
    exposure: pd.Series | None = None,
    turnover: pd.Series | None = None,
) -> dict:
    """Slice the continuous curve on [start, end] inclusive. Official n = closed round-trips with exit in window."""
    mask = (rets.index >= start) & (rets.index <= end)
    r = rets.loc[mask]
    eq = equity.reindex(r.index)
    closed = [t for t in trades if t.exit_date is not None and start <= t.exit_date <= end]
    opened = [t for t in trades if t.entry_date is not None and start <= t.entry_date <= end]
    ts = trade_stats(closed)
    ts["n_opened"] = len(opened)
    ts["n_closed"] = len(closed)
    ts["n"] = len(closed)  # official n for §8 gates: completed round-trips exiting in the window
    expo = None
    turn = None
    if exposure is not None:
        e = exposure.reindex(r.index).dropna()
        expo = float(e.mean()) if len(e) else 0.0
    if turnover is not None:
        t = turnover.reindex(r.index).dropna()
        turn = float(t.sum()) if len(t) else 0.0
    out = {
        "start": str(pd.Timestamp(start).date()),
        "end": str(pd.Timestamp(end).date()),
        "n_days": int(len(r)),
        "Sharpe": sharpe_daily_series(r),
        "CAGR": cagr(eq) if len(eq) else 0.0,
        "maxDD": max_drawdown(eq) if len(eq) else 0.0,
        "mean_ret": float(r.mean()) if len(r) else 0.0,
        "exposure": expo,
        "turnover": turn,
        "killed_in_window": False,  # filled by caller when relevant
    }
    out.update(ts)
    return out
