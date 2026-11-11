"""V2-CORE path engine, 50/50 monthly BTC/ETH benchmark, and serialisable book state.

Paper/backtest only. Capital is a required C_paper / allocated_capital value.
Kill-switch is non-sticky: flatten at -15% DD, re-arm after 30 closed 1Dutc
days flat AND BTC close > SMA200 (peak reset to current equity on re-arm).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradingbot.eval.metrics import Trade, max_drawdown, sharpe_daily_series, window_metrics
from tradingbot.strategies import spec_v2_common as P
from tradingbot.strategies.spec_v2_core import core_signals, core_target_weights


@dataclass
class BookState:
    name: str
    cash: float
    qty: dict  # symbol -> float
    peak: float
    equity: float
    killed: bool = False
    kill_pending: bool = False
    flat_days: int = 0  # consecutive closed sessions with no positions, while killed
    block_new: bool = False
    pending: dict = field(default_factory=dict)  # symbol -> target weight
    pending_signal_date: str | None = None
    last_px: dict = field(default_factory=dict)
    trades_open: dict = field(default_factory=dict)
    trades_closed: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # (date_str, equity, ret, exposure)
    killed_on: str | None = None
    rearmed_on: list = field(default_factory=list)
    last_processed: str | None = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "cash": self.cash,
            "qty": {k: float(v) for k, v in self.qty.items()},
            "peak": self.peak,
            "equity": self.equity,
            "killed": self.killed,
            "kill_pending": self.kill_pending,
            "flat_days": self.flat_days,
            "block_new": self.block_new,
            "pending": {k: float(v) for k, v in self.pending.items()},
            "pending_signal_date": self.pending_signal_date,
            "last_px": {k: float(v) for k, v in self.last_px.items() if v is not None and pd.notna(v)},
            "trades_open": self.trades_open,
            "trades_closed": self.trades_closed,
            "equity_curve": self.equity_curve,
            "killed_on": self.killed_on,
            "rearmed_on": self.rearmed_on,
            "last_processed": self.last_processed,
        }

    @classmethod
    def from_json(cls, d: dict) -> "BookState":
        st = cls(
            name=d["name"],
            cash=float(d["cash"]),
            qty=dict(d.get("qty") or {}),
            peak=float(d["peak"]),
            equity=float(d["equity"]),
        )
        for k in (
            "killed", "kill_pending", "flat_days", "block_new", "pending",
            "pending_signal_date", "last_px", "trades_open", "trades_closed",
            "equity_curve", "killed_on", "rearmed_on", "last_processed",
        ):
            if k in d and d[k] is not None:
                setattr(st, k, d[k])
        return st


def new_book(name: str, capital: float, symbols: list[str]) -> BookState:
    c = float(capital)
    if c <= 0:
        raise ValueError("capital must be positive")
    if abs(c - P.WEDDING_FUNDS_USDT) < 1e-9:
        raise ValueError("capital equals excluded 6,788 USDT wedding funds; refuse")
    return BookState(
        name=name,
        cash=c,
        qty={s: 0.0 for s in symbols},
        peak=c,
        equity=c,
    )


def _gross_exposure(qty: dict, px: dict) -> float:
    return sum(float(qty.get(s, 0.0)) * float(px[s]) for s in qty if s in px and pd.notna(px[s]))


def _fill_to_weights(
    st: BookState,
    target_w: dict,
    open_px: dict,
    date: pd.Timestamp,
    cost: float,
    *,
    block_new: bool,
    flatten: bool,
) -> float:
    """Trade toward target weights at open. Returns traded notional. Mutates st."""
    eq = float(st.equity)
    if eq <= 0:
        return 0.0
    traded = 0.0
    if flatten:
        target_w = {s: 0.0 for s in st.qty}
    for s, q in list(st.qty.items()):
        ox = open_px.get(s)
        if ox is None or pd.isna(ox) or float(ox) <= 0:
            continue
        ox = float(ox)
        w_tgt = float(target_w.get(s, 0.0))
        w_cur = (float(q) * ox / eq) if eq else 0.0
        if block_new and not flatten:
            if w_cur <= 1e-12 and w_tgt > 0:
                w_tgt = 0.0
            elif w_tgt > w_cur:
                w_tgt = w_cur
        desired_qty = (w_tgt * eq) / ox
        dq = desired_qty - float(q)
        notional = abs(dq) * ox
        if notional < 1e-8:
            continue
        if dq > 0:
            fill = ox * (1.0 + cost)
            st.cash -= dq * fill
            if float(q) <= 1e-12 and s not in st.trades_open:
                st.trades_open[s] = {
                    "symbol": s,
                    "entry_date": str(pd.Timestamp(date).date()),
                    "entry_px": fill,
                    "qty": 0.0,
                }
        else:
            fill = ox * (1.0 - cost)
            st.cash -= dq * fill  # dq < 0
            if desired_qty <= 1e-12 and s in st.trades_open:
                tr = st.trades_open.pop(s)
                pnl = fill / float(tr["entry_px"]) - 1.0
                st.trades_closed.append({
                    **tr,
                    "exit_date": str(pd.Timestamp(date).date()),
                    "exit_px": fill,
                    "pnl_frac": float(pnl),
                })
        st.qty[s] = float(q) + dq
        if s in st.trades_open:
            st.trades_open[s]["qty"] = st.qty[s]
        traded += notional
    return traded


def _mark(st: BookState, close_px: dict, date: pd.Timestamp, traded_notional: float) -> float:
    pos = 0.0
    for s, q in st.qty.items():
        px = close_px.get(s, st.last_px.get(s))
        if px is not None and pd.notna(px):
            st.last_px[s] = float(px)
            pos += float(q) * float(px)
    eq = st.cash + pos
    prev = st.equity
    r = (eq / prev - 1.0) if prev > 0 else 0.0
    st.equity = eq
    expo = (pos / eq) if eq else 0.0
    st.equity_curve.append({
        "date": str(pd.Timestamp(date).date()),
        "equity": eq,
        "ret": r,
        "exposure": expo,
        "turnover": (traded_notional / prev) if prev else 0.0,
    })
    return r


def step_core(
    st: BookState,
    date: pd.Timestamp,
    panel: dict,
    pre: dict,
    regime_row: pd.Series,
    *,
    cost: float = P.COST_2X,
    count_bar: bool = True,
) -> None:
    """One 1Dutc session for V2-CORE: fill pending at open, mark at close, set next targets.

    If count_bar is False (pre-T0), do not fill, do not count equity returns, only
    keep signal warm-up. Pending is not armed until the first counted close.
    """
    opens, close = panel["open"], panel["close"]
    o = {s: float(opens.at[date, s]) if s in opens.columns and pd.notna(opens.at[date, s]) else np.nan
         for s in P.CORE_UNIVERSE if s in opens.columns}
    c = {s: float(close.at[date, s]) if s in close.columns and pd.notna(close.at[date, s]) else np.nan
         for s in P.CORE_UNIVERSE if s in close.columns}

    if not count_bar:
        st.last_processed = str(pd.Timestamp(date).date())
        return

    flatten = bool(st.kill_pending or (st.killed and sum(abs(q) for q in st.qty.values()) > 0))
    if st.kill_pending:
        st.killed = True
        if st.killed_on is None:
            st.killed_on = str(pd.Timestamp(date).date())
        st.kill_pending = False
    block = st.block_new
    st.block_new = False
    target = dict(st.pending) if st.pending else {s: 0.0 for s in st.qty}
    traded = _fill_to_weights(st, target, o, date, cost, block_new=block, flatten=flatten)
    r = _mark(st, c, date, traded)

    if st.equity > st.peak:
        st.peak = st.equity
    dd = (st.equity / st.peak - 1.0) if st.peak > 0 else 0.0
    if (not st.killed) and dd <= P.CORE_KILL_DD:
        st.kill_pending = True
    if r <= P.CORE_DAILY_LOSS_CAP:
        st.block_new = True

    # Re-arm: after flatten, count flat closed sessions; re-arm when >=30 and BTC > SMA200.
    held = sum(abs(float(q)) for q in st.qty.values()) > 1e-12
    if st.killed:
        if held:
            st.flat_days = 0
        else:
            st.flat_days += 1
            btc_px = c.get("BTC")
            s200 = pre["sma200"]["BTC"].at[date] if "BTC" in pre["sma200"].columns else np.nan
            if st.flat_days >= P.CORE_REARM_FLAT_DAYS and pd.notna(btc_px) and pd.notna(s200) and float(btc_px) > float(s200):
                st.killed = False
                st.kill_pending = False
                st.flat_days = 0
                st.peak = st.equity  # new cycle
                st.rearmed_on.append(str(pd.Timestamp(date).date()))

    # Next targets from today's close
    if st.killed or st.kill_pending:
        st.pending = {s: 0.0 for s in st.qty}
    else:
        state = str(regime_row.get("macroy_state", "NORMAL"))
        cur_w = pd.Series(
            {s: (float(st.qty.get(s, 0.0)) * float(c[s]) / st.equity) if st.equity and pd.notna(c.get(s)) else 0.0
             for s in P.CORE_UNIVERSE},
        )
        w = core_target_weights(date, panel, pre, cur_w, state)
        st.pending = {s: float(w.get(s, 0.0)) for s in w.index}
        st.pending_signal_date = str(pd.Timestamp(date).date())
    st.last_processed = str(pd.Timestamp(date).date())


def step_bench(
    st: BookState,
    date: pd.Timestamp,
    panel: dict,
    *,
    cost: float = P.COST_2X,
    count_bar: bool = True,
    is_first_counted: bool = False,
) -> None:
    """50/50 BTC/ETH buy-and-hold, rebalanced on the first 1Dutc bar of each UTC month."""
    opens, close = panel["open"], panel["close"]
    o = {s: float(opens.at[date, s]) if s in opens.columns and pd.notna(opens.at[date, s]) else np.nan
         for s in P.CORE_UNIVERSE}
    c = {s: float(close.at[date, s]) if s in close.columns and pd.notna(close.at[date, s]) else np.nan
         for s in P.CORE_UNIVERSE}
    if not count_bar:
        st.last_processed = str(pd.Timestamp(date).date())
        return
    rebalance = is_first_counted or (pd.Timestamp(date).day == 1)
    target = {s: P.BENCH_WEIGHT for s in P.CORE_UNIVERSE} if rebalance else {
        s: (float(st.qty.get(s, 0.0)) * float(o[s]) / st.equity) if st.equity and pd.notna(o.get(s)) else 0.0
        for s in P.CORE_UNIVERSE
    }
    # If not rebalancing, skip trades (hold). Still mark.
    traded = 0.0
    if rebalance:
        traded = _fill_to_weights(st, target, o, date, cost, block_new=False, flatten=False)
    _mark(st, c, date, traded)
    if st.equity > st.peak:
        st.peak = st.equity
    st.last_processed = str(pd.Timestamp(date).date())


def curve_series(st: BookState) -> tuple[pd.Series, pd.Series]:
    if not st.equity_curve:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    idx = pd.to_datetime([r["date"] for r in st.equity_curve])
    eq = pd.Series([r["equity"] for r in st.equity_curve], index=idx, name="equity")
    rets = pd.Series([r["ret"] for r in st.equity_curve], index=idx, name="ret")
    return eq, rets


def run_core_path(
    panel: dict,
    pre: dict,
    regime: pd.DataFrame,
    capital: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    cost: float = P.COST_2X,
) -> BookState:
    st = new_book("V2-CORE", capital, list(P.CORE_UNIVERSE))
    dates = panel["close"].index[(panel["close"].index >= start) & (panel["close"].index <= end)]
    first = True
    # Arm pending from the close immediately before start, so first fill is at start's open.
    pre_dates = panel["close"].index[panel["close"].index < start]
    if len(pre_dates):
        t0 = pre_dates[-1]
        row = regime.loc[t0] if t0 in regime.index else pd.Series({"macroy_state": "NORMAL"})
        # Temporarily mark as counted=False then set pending from t0
        cur = pd.Series(0.0, index=list(P.CORE_UNIVERSE))
        state = str(row.get("macroy_state", "NORMAL")) if "macroy_state" in row.index else "NORMAL"
        w = core_target_weights(t0, panel, pre, cur, state)
        st.pending = {s: float(w.get(s, 0.0)) for s in w.index}
        st.pending_signal_date = str(pd.Timestamp(t0).date())
    for dt in dates:
        step_core(st, dt, panel, pre, regime.loc[dt], cost=cost, count_bar=True)
        first = False
    return st


def run_bench_path(
    panel: dict,
    capital: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    cost: float = P.COST_2X,
) -> BookState:
    st = new_book("BENCH-50-50", capital, list(P.CORE_UNIVERSE))
    dates = panel["close"].index[(panel["close"].index >= start) & (panel["close"].index <= end)]
    first = True
    for dt in dates:
        step_bench(st, dt, panel, cost=cost, count_bar=True, is_first_counted=first)
        first = False
    return st
