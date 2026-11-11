"""v3 metrics: DSR, trade-order Monte Carlo, regime breakdown. Reuses v1 Sharpe/DSR defs."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from tradingbot.eval.metrics import (
    ANN,
    cagr,
    daily_sharpe_raw,
    dsr_from_returns,
    max_drawdown,
    sharpe_daily_series,
    trade_stats,
)
from tradingbot.universe import sma

MC_DEFAULT_N = 5000
MC_SEED = 42


def exposure_turnover(curve_rows: list[dict] | None, rets: pd.Series, book=None) -> tuple[pd.Series, pd.Series]:
    if book is not None and getattr(book, "equity_curve", None):
        idx = pd.to_datetime([r["date"] for r in book.equity_curve])
        expo = pd.Series([r.get("exposure", 0.0) for r in book.equity_curve], index=idx)
        turn = pd.Series([r.get("turnover", 0.0) for r in book.equity_curve], index=idx)
        return expo, turn
    if curve_rows:
        idx = pd.to_datetime([r["date"] for r in curve_rows])
        expo = pd.Series([r.get("exposure", 0.0) for r in curve_rows], index=idx)
        turn = pd.Series([r.get("turnover", 0.0) for r in curve_rows], index=idx)
        return expo, turn
    z = pd.Series(0.0, index=rets.index)
    return z, z.copy()


def trades_from_book(book) -> list[dict]:
    rows = []
    for t in getattr(book, "trades_closed", []) or []:
        rows.append(
            {
                "symbol": t.get("symbol"),
                "entry_date": t.get("entry_date"),
                "exit_date": t.get("exit_date"),
                "entry_px": t.get("entry_px"),
                "exit_px": t.get("exit_px"),
                "pnl_frac": t.get("pnl_frac"),
            }
        )
    return rows


def profit_factor(pnls: Iterable[float]) -> float | None:
    xs = [float(p) for p in pnls]
    if not xs:
        return None
    pos = sum(p for p in xs if p > 0)
    neg = -sum(p for p in xs if p <= 0)
    if neg > 0:
        return pos / neg
    return math.inf if pos > 0 else None


def summarize(
    equity: pd.Series,
    rets: pd.Series,
    trades: list[dict] | None = None,
    exposure: pd.Series | None = None,
    turnover: pd.Series | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict:
    rets = rets.dropna().astype(float)
    if start is not None:
        rets = rets[(rets.index >= start) & (rets.index <= end)]
    equity = equity.reindex(rets.index)
    trades = trades or []
    if start is not None:
        closed = [
            t
            for t in trades
            if t.get("exit_date") is not None
            and start <= pd.Timestamp(t["exit_date"]) <= (end if end is not None else pd.Timestamp(t["exit_date"]))
        ]
    else:
        closed = [t for t in trades if t.get("exit_date") is not None and t.get("pnl_frac") is not None]
    pnls = [float(t["pnl_frac"]) for t in closed if t.get("pnl_frac") is not None]
    expo = exposure.reindex(rets.index) if exposure is not None else None
    turn = turnover.reindex(rets.index) if turnover is not None else None
    end_eq = float(equity.iloc[-1]) if len(equity) else None
    start_eq = float(equity.iloc[0]) if len(equity) else None
    # window_metrics uses first/last in window; if the curve is continuous, return is last/first_of_full?
    # Use last / first in slice. For a sliced continuous path, first point already includes prior PnL;
    # report both end equity and slice return from the first return in the window.
    total_ret = float((1.0 + rets).prod() - 1.0) if len(rets) else 0.0
    return {
        "Sharpe": sharpe_daily_series(rets),
        "maxDD": max_drawdown(equity) if len(equity) else 0.0,
        "CAGR": cagr(equity) if len(equity) else 0.0,
        "total_return": total_ret,
        "end_equity": end_eq,
        "n_days": int(len(rets)),
        "n_closed_trades": len(closed),
        "PF": profit_factor(pnls),
        "win_rate": (float(np.mean([p > 0 for p in pnls])) if pnls else None),
        "avg_trade_pct": (float(np.mean(pnls)) if pnls else None),
        "mean_exposure": (float(expo.mean()) if expo is not None and len(expo) else None),
        "mean_turnover": (float(turn.mean()) if turn is not None and len(turn) else None),
        "start": str(rets.index[0].date()) if len(rets) else None,
        "end": str(rets.index[-1].date()) if len(rets) else None,
    }


def dsr_for(rets: pd.Series, sharpes_daily: list[float], N: int) -> dict:
    var = float(np.var(sharpes_daily, ddof=1)) if len(sharpes_daily) > 1 else 0.0
    return dsr_from_returns(rets, sr_trials_var=var, N=N)


def monte_carlo_trade_order(
    pnls: list[float],
    capital: float,
    n: int = MC_DEFAULT_N,
    seed: int = MC_SEED,
) -> dict:
    """Reshuffle closed-trade returns; compound from `capital`. Returns percentile stats."""
    xs = np.asarray(pnls, dtype=float)
    if xs.size == 0 or capital <= 0:
        return {"n_trades": int(xs.size), "n_paths": 0, "note": "no trades"}
    rng = np.random.default_rng(seed)
    finals = np.empty(n, dtype=float)
    maxdds = np.empty(n, dtype=float)
    for i in range(n):
        order = rng.permutation(xs)
        eq = capital * np.cumprod(1.0 + order)
        # include starting capital as the peak base
        path = np.concatenate([[capital], eq])
        finals[i] = path[-1]
        peaks = np.maximum.accumulate(path)
        maxdds[i] = float((path / peaks - 1.0).min())
    def pct(arr, q):
        return float(np.percentile(arr, q))
    return {
        "n_trades": int(xs.size),
        "n_paths": int(n),
        "seed": int(seed),
        "capital": float(capital),
        "final_equity": {
            "p5": pct(finals, 5),
            "p25": pct(finals, 25),
            "p50": pct(finals, 50),
            "p75": pct(finals, 75),
            "p95": pct(finals, 95),
            "mean": float(finals.mean()),
        },
        "maxDD": {
            "p5": pct(maxdds, 5),  # shallow (close to 0) is high percentile? p5 is worse (more negative)
            "p25": pct(maxdds, 25),
            "p50": pct(maxdds, 50),
            "p75": pct(maxdds, 75),
            "p95": pct(maxdds, 95),
            "mean": float(maxdds.mean()),
        },
    }


def regime_labels(btc_close: pd.Series) -> pd.Series:
    """BULL / BEAR / CHOP from BTC vs SMA200 and 20d vol vs 1y median."""
    s200 = sma(btc_close, 200)
    lr = np.log(btc_close).diff()
    vol20 = lr.rolling(20, min_periods=10).std() * math.sqrt(365.0)
    vol_med = vol20.rolling(365, min_periods=90).median()
    labels = pd.Series("CHOP", index=btc_close.index, dtype=object)
    above = btc_close > s200
    high_vol = vol20 > vol_med
    labels = labels.mask(above.fillna(False) & (~high_vol.fillna(False)), "BULL")
    labels = labels.mask(above.fillna(False) & high_vol.fillna(False), "CHOP")
    labels = labels.mask((~above.fillna(False)) & s200.notna(), "BEAR")
    # warmup: SMA200 missing → NA
    labels = labels.mask(s200.isna(), pd.NA)
    return labels


def regime_breakdown(rets: pd.Series, labels: pd.Series, exposure: pd.Series | None = None) -> dict:
    out = {}
    lab = labels.reindex(rets.index)
    for name in ("BULL", "BEAR", "CHOP"):
        mask = lab == name
        r = rets[mask]
        item = {
            "n_days": int(mask.sum()),
            "Sharpe": sharpe_daily_series(r) if len(r) else None,
            "total_return": float((1.0 + r.astype(float)).prod() - 1.0) if len(r) else None,
            "mean_ret": float(r.mean()) if len(r) else None,
        }
        if exposure is not None:
            item["mean_exposure"] = float(exposure.reindex(rets.index)[mask].mean()) if mask.any() else None
        out[name] = item
    return out


def slice_curve(equity: pd.Series, rets: pd.Series, start, end) -> tuple[pd.Series, pd.Series]:
    mask = (rets.index >= pd.Timestamp(start)) & (rets.index <= pd.Timestamp(end))
    return equity.loc[mask], rets.loc[mask]
