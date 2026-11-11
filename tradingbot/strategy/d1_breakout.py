"""Reference D1 plugin: 20-day close breakout + mid-channel ratchet stop.

Logic COPIED from /workspace/crypto/research4.py `a4_soozie` (exit='mid') as wrapped by
daily_bo.py. Source files are not modified. This is a reference plugin, not a proven edge.

Entry (closed 1Dutc): in point-in-time monthly top-20, BTC close > SMA200, breadth >= 50%,
close[t] > max(close[t-1]..close[t-20]), mid-stop < close.
Stop: mid of last 20 closes, ratchets up only; exit when close < stop or BTC gate off.
Size: min(1% equity / stop-distance, 10% of allocated/paper capital); gross cap 100%.
SPOT long only — no shorts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from tradingbot.regime import btc_above_sma200
from tradingbot.types import Signal
from tradingbot.universe import (
    B_SLOTS,
    breadth_top20,
    monthly_top20_universe,
    sma,
)

PLUGIN_NAME = "REF_d1_a4_soozie"
DISCLAIMER = (
    "REFERENCE plugin ported from research4.a4_soozie / daily_bo.py. "
    "Not a proven edge. Results are not a claim of profitability."
)


@dataclass
class OpenTrade:
    inst_id: str
    entry: float
    stop: float
    risk: float
    size: float  # weight of equity
    since: object
    signal_bar: int


class D1BreakoutStrategy:
    """Cross-sectional daily plugin. Call `prepare(panel)` once, then `signals_at(t)` / `on_index`."""

    name = PLUGIN_NAME
    bar = "1Dutc"

    def __init__(self, allocated_capital: float, exit_mode: str = "mid"):
        if allocated_capital is None or allocated_capital <= 0:
            raise ValueError("D1 plugin requires allocated_capital; never sizes from OKX account balance")
        self.allocated_capital = float(allocated_capital)
        self.exit_mode = exit_mode
        self.C = self.HI = self.LO = self.U = None
        self.breadth = None
        self.gate = None
        self._ready = False

    def warmup_bars(self) -> int:
        return 220  # SMA200 + 20-day channel

    def prepare(
        self,
        close: pd.DataFrame,
        high: Optional[pd.DataFrame] = None,
        low: Optional[pd.DataFrame] = None,
        quote_vol: Optional[pd.DataFrame] = None,
        universe: Optional[pd.DataFrame] = None,
        breadth: Optional[pd.Series] = None,
    ) -> None:
        if "BTC" not in close.columns:
            raise ValueError("D1 plugin needs a BTC column in the close panel")
        self.C = close
        self.HI = high if high is not None else close
        self.LO = low if low is not None else close
        VQ = quote_vol if quote_vol is not None else pd.DataFrame(0.0, close.index, close.columns)
        self.U = universe if universe is not None else monthly_top20_universe(close, VQ)
        self.breadth = breadth if breadth is not None else breadth_top20(close, self.U)
        self.gate = btc_above_sma200(close["BTC"]).astype(float).reindex(close.index).fillna(0.0)
        self._ready = True

    def weights_and_trades(self, state: Optional[dict] = None):
        """Port of a4_soozie(g, 'mid', state=state). Returns (weight DataFrame, trades list)."""
        if not self._ready:
            raise RuntimeError("call prepare() first")
        C, U = self.C, self.U
        g = self.gate
        br = self.breadth.reindex(C.index).values
        gg = g.values
        w = pd.DataFrame(0.0, C.index, C.columns)
        trades = []
        for s in C.columns:
            c = C[s].values
            u = U[s].values
            prevmax = C[s].shift(1).rolling(20).max().values
            mx = C[s].rolling(20).max().values
            mn = C[s].rolling(20).min().values
            pos = False
            st = 0.0
            ep = 0.0
            risk = 0.0
            t0 = None
            col = np.zeros(len(c))
            size = 0.0
            for t in range(len(c)):
                if np.isnan(c[t]):
                    pos = False
                    continue
                stop_t = (mx[t] + mn[t]) / 2.0
                if pos:
                    if not np.isnan(stop_t):
                        st = max(st, stop_t)  # ratchet up only
                    if c[t] < st or gg[t] == 0:
                        pos = False
                        # trade close recorded by the harness at this bar's close
                        trades.append(
                            {
                                "sym": s,
                                "entry_idx": t0,
                                "exit_idx": t,
                                "entry": ep,
                                "exit": c[t],
                                "risk": risk,
                                "size": size,
                            }
                        )
                elif (
                    u[t]
                    and gg[t] > 0
                    and br[t] >= 0.5
                    and not np.isnan(prevmax[t])
                    and c[t] > prevmax[t]
                    and not np.isnan(stop_t)
                    and stop_t < c[t]
                ):
                    pos = True
                    ep = c[t]
                    st = stop_t
                    risk = (ep - st) / ep
                    t0 = t
                    size = min(0.01 / risk, 0.10)
                col[t] = size if pos else 0.0
            w[s] = col
            if state is not None and pos:
                state[s] = dict(
                    entry=ep,
                    stop=st,
                    risk=risk,
                    size=size,
                    since=C.index[t0],
                    new=t0 == len(c) - 1,
                    close=c[-1],
                )
        # cap_total: scale down if gross weight > 1
        ssum = w.sum(axis=1)
        w = w.div(np.maximum(ssum, 1.0), axis=0)
        # gate flatten already handled inside the loop (gg[t]==0 exits)
        w = w.mul(g.where(g > 0, 0.0), axis=0)
        return w, trades

    def signals_at(self, ts, weights_prev, weights_now, stops: dict) -> list[Signal]:
        """Translate a weight change at bar ts into Signal objects (buy/close)."""
        sigs: list[Signal] = []
        # pandas Timestamp
        ts_ms = int(pd.Timestamp(ts).value // 1_000_000)
        for s in weights_now.index:
            inst = s if str(s).endswith("-USDT") else f"{s}-USDT"
            w0 = float(weights_prev.get(s, 0.0) or 0.0)
            w1 = float(weights_now.get(s, 0.0) or 0.0)
            if w0 <= 0 and w1 > 0:
                st = stops.get(s)
                sigs.append(
                    Signal(
                        inst_id=inst,
                        side="buy",
                        ts=ts_ms,
                        signal_id=f"{self.name}:{inst}:{ts_ms}:buy",
                        stop_price=st,
                        reason="d1 20-day close breakout (reference a4_soozie)",
                        strategy=self.name,
                        strength=w1,
                    )
                )
            elif w0 > 0 and w1 <= 0:
                sigs.append(
                    Signal(
                        inst_id=inst,
                        side="close",
                        ts=ts_ms,
                        signal_id=f"{self.name}:{inst}:{ts_ms}:close",
                        reason="d1 ratchet mid-stop or BTC SMA200 gate off",
                        strategy=self.name,
                    )
                )
        return sigs
