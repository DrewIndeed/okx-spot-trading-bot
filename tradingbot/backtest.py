"""Walk-forward backtest harness. Up to 3 strategy variants side by side.

Costs: base taker fee 0.1%/side, slippage = slippage_mult * fee (default 2x fee).
Also reports a 2x-fee stress run. SPOT long only. Closed candles only.

Results are for the configured placeholder / reference plugins — not a claim of profitability.
Equity always starts at allocated_capital (never an OKX account balance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from tradingbot.config import Config
from tradingbot.orders import OrderManager
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskManager
from tradingbot.strategy.placeholder import PLACEHOLDER_DISCLAIMER, PlaceholderEmaAtrStrategy
from tradingbot.types import Candle, Fill, OrderIntent, Position, Signal

DISCLAIMER = (
    "PLACEHOLDER / REFERENCE strategy results only. Not a proven edge. "
    "Numbers are not a claim of profitability. In-sample and out-of-sample are reported separately."
)


@dataclass
class ClosedTrade:
    inst_id: str
    entry_ts: int
    exit_ts: int
    entry_px: float
    exit_px: float
    qty: float
    pnl: float
    r_multiple: Optional[float]
    reason: str = ""


@dataclass
class Metrics:
    n: int = 0
    pf: Optional[float] = None
    max_dd: float = 0.0
    exp_r: Optional[float] = None
    total_pnl: float = 0.0
    end_equity: Optional[float] = None
    start_equity: Optional[float] = None

    def as_dict(self) -> dict:
        def f(x):
            if x is None:
                return None
            if x == float("inf"):
                return "inf"
            return float(x)

        return {
            "n_trades": self.n,
            "pf": f(self.pf),
            "max_dd": float(self.max_dd),
            "exp_r": f(self.exp_r),
            "total_pnl": float(self.total_pnl),
            "end_equity": f(self.end_equity),
            "start_equity": f(self.start_equity),
        }


def max_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    dd = 0.0
    for x in equity:
        if x > peak:
            peak = x
        if peak > 0:
            dd = min(dd, (x - peak) / peak)
    return abs(dd)


def compute_metrics(
    trades: Sequence[ClosedTrade],
    equity: Sequence[float],
    start_equity: Optional[float] = None,
) -> Metrics:
    pnls = [t.pnl for t in trades]
    wins = sum(p for p in pnls if p > 0)
    losses = sum(p for p in pnls if p < 0)
    if losses < 0 and wins > 0:
        pf = wins / abs(losses)
    elif losses == 0 and wins > 0:
        pf = float("inf")
    else:
        pf = None
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    exp_r = float(np.mean(rs)) if rs else None
    end = float(equity[-1]) if equity else start_equity
    start = start_equity if start_equity is not None else (float(equity[0]) if equity else None)
    return Metrics(
        n=len(trades),
        pf=pf,
        max_dd=max_drawdown(equity),
        exp_r=exp_r,
        total_pnl=float(sum(pnls)),
        end_equity=end,
        start_equity=start,
    )


def walk_forward_bounds(n: int, n_folds: int = 4, oos_ratio: float = 0.20) -> list[tuple[int, int, int]]:
    """Return list of (is_end, oos_start, oos_end) with expanding IS. Indices are half-open."""
    if n_folds < 1 or n < 10:
        return [(max(1, int(n * (1 - oos_ratio))), int(n * (1 - oos_ratio)), n)]
    oos_total = max(n_folds, int(n * oos_ratio))
    fold = oos_total // n_folds
    start_oos = n - fold * n_folds
    out = []
    for k in range(n_folds):
        oos_s = start_oos + k * fold
        oos_e = oos_s + fold if k < n_folds - 1 else n
        out.append((oos_s, oos_s, oos_e))  # expanding IS = [0, oos_s)
    return out


def _fmt(m: Metrics) -> str:
    def num(x, d=2):
        if x is None:
            return "n/a"
        if x == float("inf"):
            return "inf"
        return f"{x:.{d}f}"

    return f"PF={num(m.pf)}  maxDD={m.max_dd:.1%}  expR={num(m.exp_r)}  N={m.n}"


# ---- 4H / 1H single-asset placeholder sim (entry = next bar open, SL first) ----

def simulate_placeholder(
    candles: Sequence[Candle],
    cfg: Config,
    strategy: Optional[PlaceholderEmaAtrStrategy] = None,
    *,
    fee: Optional[float] = None,
    slippage: Optional[float] = None,
) -> tuple[list[ClosedTrade], list[float], list[int]]:
    """Causal next-open entry, stop-first exits. Equity starts at allocated_capital."""
    strategy = strategy or PlaceholderEmaAtrStrategy()
    fee = cfg.fee_taker if fee is None else fee
    slip = cfg.slippage if slippage is None else slippage
    broker = OrderManager(fee_taker=fee, slippage=slip)
    risk = RiskManager(cfg.risk, cfg.allocated_capital)
    book = Portfolio.new(cfg.allocated_capital)
    closed: list[ClosedTrade] = []
    equity: list[float] = []
    equity_ts: list[int] = []
    pending: Optional[Signal] = None  # fill at next open
    bars = [c for c in candles if c.confirm == 1]
    warm = strategy.warmup_bars()
    n = len(bars)

    def mark(i: int) -> dict:
        return {cfg.symbol: bars[i].close}

    def flatten_at(i: int, px: float, reason: str, is_stop: bool = False) -> None:
        nonlocal book
        pos = book.positions.get(cfg.symbol)
        if not pos or pos.qty <= 0:
            return
        intent = OrderIntent(
            inst_id=cfg.symbol,
            side="sell",
            qty=pos.qty,
            price=px,
            ts=bars[i].ts,
            signal_id=f"{pos.signal_id}:exit:{bars[i].ts}",
            client_oid=f"ex{pos.client_oid}"[:32],
            is_exit=True,
            reason=reason,
            initial_risk=pos.initial_risk,
        )
        # stop fills: if we already chose the stop/gap price, still apply slip on it (conservative)
        fill = broker.submit_paper(book, intent)
        closed.append(
            ClosedTrade(
                inst_id=cfg.symbol,
                entry_ts=pos.opened_ts,
                exit_ts=bars[i].ts,
                entry_px=pos.avg_px,
                exit_px=fill.price,
                qty=fill.qty,
                pnl=fill.pnl,
                r_multiple=fill.r_multiple,
                reason=reason,
            )
        )

    for i in range(n):
        bar = bars[i]
        # 1) pending entry from previous signal, fill at this open
        if pending is not None and pending.side == "buy" and cfg.symbol not in book.positions:
            decision = risk.evaluate(pending, book, bar.open, marks=mark(i))
            if decision.allowed:
                intent = OrderIntent(
                    inst_id=cfg.symbol,
                    side="buy",
                    qty=decision.qty,
                    price=bar.open,
                    ts=bar.ts,
                    signal_id=pending.signal_id,
                    client_oid=decision.client_oid,
                    stop=pending.stop_price,
                    take_profit=pending.take_profit,
                    initial_risk=decision.initial_risk,
                    reason=pending.reason,
                )
                broker.submit_paper(book, intent)
            pending = None
        elif pending is not None and pending.side == "close":
            flatten_at(i, bar.open, pending.reason or "signal close")
            pending = None

        # 2) stop check on this bar (after entry fill)
        pos = book.positions.get(cfg.symbol)
        if pos and pos.qty > 0 and pos.stop is not None and bar.low <= pos.stop:
            # same conservative rule as /workspace/crypto/backtest.py
            just_entered = pos.opened_ts == bar.ts
            px = pos.stop if just_entered else min(bar.open, pos.stop)
            flatten_at(i, px, "atr stop")

        # 3) signal on this close (for next bar), skip last bar so we never need a future open
        if i >= warm and i < n - 1:
            pos_now = book.positions.get(cfg.symbol)
            sig = strategy.on_candles(cfg.symbol, bars[: i + 1], pos_now)
            pending = sig

        eq = book.equity(mark(i))
        book.update_peaks(eq, day="")
        risk.refresh_halts(book, mark(i))
        equity.append(eq)
        equity_ts.append(bar.ts)

    # flatten any leftover at last close (end of window, not a strategy exit)
    if cfg.symbol in book.positions:
        flatten_at(n - 1, bars[-1].close, "end_of_window")
        equity[-1] = book.equity({cfg.symbol: bars[-1].close})
    return closed, equity, equity_ts


def fold_metrics(
    trades: Sequence[ClosedTrade],
    equity: Sequence[float],
    equity_ts: Sequence[int],
    bars: Sequence[Candle],
    n_folds: int,
    oos_ratio: float,
    start_equity: float,
) -> list[dict]:
    n = len(bars)
    bounds = walk_forward_bounds(n, n_folds, oos_ratio)
    # map ts -> index
    ts_to_i = {c.ts: i for i, c in enumerate(bars)}
    rows = []
    for k, (is_end, oos_s, oos_e) in enumerate(bounds, 1):
        def select(t0, t1):
            ts0 = bars[t0].ts if t0 < n else bars[-1].ts
            ts1 = bars[t1 - 1].ts if t1 > 0 else bars[0].ts
            tr = [t for t in trades if ts0 <= t.entry_ts <= ts1]
            # equity slice by index
            eq = list(equity[t0:t1]) if t1 > t0 else []
            # start equity of slice
            s0 = equity[t0] if t0 < len(equity) else start_equity
            return compute_metrics(tr, eq, start_equity=s0)

        is_m = select(0, is_end)
        oos_m = select(oos_s, oos_e)
        rows.append(
            {
                "fold": k,
                "is_bars": [0, is_end],
                "oos_bars": [oos_s, oos_e],
                "is": is_m,
                "oos": oos_m,
                "is_range": (bars[0].ts, bars[is_end - 1].ts if is_end else bars[0].ts),
                "oos_range": (bars[oos_s].ts, bars[min(oos_e, n) - 1].ts),
            }
        )
    return rows


def format_report(
    title: str,
    candles: Sequence[Candle],
    trades: Sequence[ClosedTrade],
    equity: Sequence[float],
    equity_ts: Sequence[int],
    cfg: Config,
    folds: list[dict],
    *,
    stress: Optional[tuple[list[ClosedTrade], list[float], list[dict]]] = None,
    extra_note: str = "",
) -> str:
    bars = [c for c in candles if c.confirm == 1]
    full = compute_metrics(trades, equity, start_equity=cfg.allocated_capital)
    oos_all = []
    oos_eq_parts = []
    for f in folds:
        oos_all.extend(
            # reconstruct from metrics only? better collect trades in fold dict
            []
        )
    # pooled OOS trades: those with entry in any OOS window
    oos_trades = []
    for f in folds:
        a, b = f["oos_bars"]
        ts0, ts1 = bars[a].ts, bars[min(b, len(bars)) - 1].ts
        oos_trades.extend([t for t in trades if ts0 <= t.entry_ts <= ts1])
    # stitched OOS equity is not a single continuous account; report pooled trade metrics + per-fold DD
    pooled = compute_metrics(oos_trades, [cfg.allocated_capital], start_equity=cfg.allocated_capital)
    # use max of fold OOS DDs for combined DD note
    max_oos_dd = max((f["oos"].max_dd for f in folds), default=0.0)
    pooled.max_dd = max_oos_dd

    def tsfmt(ms):
        import datetime
        return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        DISCLAIMER,
        extra_note,
        f"Run: {title}",
        f"Window: {tsfmt(bars[0].ts)} -> {tsfmt(bars[-1].ts)}  ({len(bars)} closed bars)",
        f"allocated_capital / paper_notional_usdt: {cfg.allocated_capital:.2f} USDT "
        f"(dedicated sub-account config; NOT OKX main balance)",
        f"Fee: {cfg.fee_taker:.2%} /side   Slippage: {cfg.slippage:.2%} /side "
        f"(slippage_mult={cfg.slippage_mult}x fee)",
        f"Full-window (informational, includes IS): {_fmt(full)}  end_eq={full.end_equity:.2f}",
        "Walk-forward folds (expanding IS, metrics on each segment):",
    ]
    for f in folds:
        is_a, is_b = f["is_range"]
        oos_a, oos_b = f["oos_range"]
        lines.append(
            f"  Fold {f['fold']}  IS[{tsfmt(is_a)} -> {tsfmt(is_b)}]: {_fmt(f['is'])}"
        )
        lines.append(
            f"         OOS[{tsfmt(oos_a)} -> {tsfmt(oos_b)}]: {_fmt(f['oos'])}"
        )
    lines.append(f"Pooled OOS trades (max fold OOS DD): {_fmt(pooled)}")
    if stress is not None:
        st_trades, st_eq, st_folds = stress
        st_full = compute_metrics(st_trades, st_eq, start_equity=cfg.allocated_capital)
        st_oos = []
        for f in st_folds:
            a, b = f["oos_bars"]
            ts0, ts1 = bars[a].ts, bars[min(b, len(bars)) - 1].ts
            st_oos.extend([t for t in st_trades if ts0 <= t.entry_ts <= ts1])
        st_pooled = compute_metrics(st_oos, [cfg.allocated_capital], start_equity=cfg.allocated_capital)
        st_pooled.max_dd = max((f["oos"].max_dd for f in st_folds), default=0.0)
        lines.append("2x-fee STRESS (fee=2× base, slippage scales with fee):")
        lines.append(f"  Full-window: {_fmt(st_full)}  end_eq={st_full.end_equity:.2f}")
        for f in st_folds:
            lines.append(f"  Fold {f['fold']}  IS: {_fmt(f['is'])}  |  OOS: {_fmt(f['oos'])}")
        lines.append(f"  Pooled OOS: {_fmt(st_pooled)}")
    return "\n".join(x for x in lines if x is not None and x != "")


# ---- D1 panel simulation (close-to-close, costs on turnover; port of a4_soozie path) ----

def simulate_d1(weights: "object", close: "object", cfg: Config, fee: float, slip: float, trades_meta: list) -> tuple[list[ClosedTrade], list[float], list[int]]:
    """Apply cap-total weights at close t, earn close t -> t+1, charge fee+slip on turnover.

    Mirrors research4/research2 COST-on-turnover, but with explicit fee and slippage.
    Equity starts at allocated_capital.
    """
    import pandas as pd

    C = close
    w = weights.reindex(C.index).fillna(0.0)
    cost = fee + slip  # per side, applied on turnover (like r2.COST if fee+slip=0.002)
    cap = cfg.allocated_capital
    equity = [cap]
    eq_ts = [int(pd.Timestamp(C.index[0]).value // 1_000_000)]
    cash_eq = cap
    closed: list[ClosedTrade] = []
    # map trades_meta by (sym, exit_idx)
    by_exit = {}
    for t in trades_meta:
        by_exit.setdefault((t["sym"], t["exit_idx"]), []).append(t)

    prev_w = w.iloc[0] * 0.0
    # holdings in units of weight * equity / price at last rebalance... we track equity via returns
    # r_t = sum w_{t-1} * (c_t/c_{t-1} - 1) - cost * turnover_t
    for i in range(1, len(C)):
        ret = (C.iloc[i] / C.iloc[i - 1] - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        gross = float((prev_w * ret).sum())
        turn = float((w.iloc[i] - prev_w).abs().sum())  # one-way? research uses 0.5 * abs sum? 
        # research4 rets: typically cost * (w.diff.abs sum) — check research2
        # research: (1-COST)**2 on a round trip; daily rets use cost * turnover.
        # We'll charge `cost` per unit of absolute weight change (each side once).
        net = gross - cost * turn
        cash_eq = cash_eq * (1.0 + net)
        equity.append(cash_eq)
        eq_ts.append(int(pd.Timestamp(C.index[i]).value // 1_000_000))
        # closed trades at this index
        for s in C.columns:
            recs = by_exit.get((s, i), [])
            for rec in recs:
                # pnl in USDT approx size * allocated path is path-dependent; use trade R and weight at entry
                entry, exit_px = rec["entry"], rec["exit"]
                risk = rec["risk"] or 0.0
                size = rec["size"] or 0.0
                # net ret after both sides
                r_net = (exit_px / entry) * (1.0 - cost) ** 2 - 1.0 if entry else 0.0
                # notional at entry roughly size * equity_at_entry; use start cap * size as conservative report
                # Better: size is weight; pnl ≈ r_net * size * equity_at_entry. Use equity[rec['entry_idx']]
                e0 = equity[rec["entry_idx"]] if rec["entry_idx"] < len(equity) else cap
                pnl = r_net * size * e0
                r_mult = ((exit_px - entry) / entry - 2 * cost) / risk if risk else None
                closed.append(
                    ClosedTrade(
                        inst_id=f"{s}-USDT",
                        entry_ts=int(pd.Timestamp(C.index[rec["entry_idx"]]).value // 1_000_000),
                        exit_ts=eq_ts[-1],
                        entry_px=entry,
                        exit_px=exit_px,
                        qty=0.0,
                        pnl=pnl,
                        r_multiple=r_mult,
                        reason="d1 mid-stop or gate",
                    )
                )
        prev_w = w.iloc[i]
    return closed, equity, eq_ts
