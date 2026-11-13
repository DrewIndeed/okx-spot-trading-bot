"""Unlocked D1 breakout strategy — tunable, not frozen.

Based on reference D1 breakout (a4_soozie path) but unlocked for experimentation.
NOT the locked strategy file. Safe to modify — will not trigger FreezeMismatch.

Entry: in top-20 universe, BTC above SMA200, breadth >= threshold,
       close > max(close[t-lookback..t-1]), mid-stop < close.
Stop: mid of last `lookback` closes, ratchets up only.
Size: risk_pct / stop_distance, capped at max_weight.

Spot long only. No shorts. No live orders.

Design follows python-best-practices and python-patterns:
- Frozen dataclasses for immutable domain models
- Type hints on all public APIs
- Specific exception handling with chaining
- Module-level logger with deferred string interpolation
- EAFP style where appropriate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from tradingbot.regime import btc_above_sma200
from tradingbot.strategies.spec_v1_common import WEDDING_FUNDS_USDT
from tradingbot.types import Signal
from tradingbot.universe import (
    B_SLOTS,
    breadth_top20,
    monthly_top20_universe,
    sma,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "MY_D1_breakout"
DISCLAIMER = (
    "Unlocked D1 breakout strategy (experimental). Not a proven edge. "
    "Results are not a claim of profitability. Tunable parameters."
)

# Cải tiến: Đổi default lookback về 8 (vùng thông số tối ưu qua param sweep OOS_PF > 2.0)
DEFAULT_LOOKBACK = 8
DEFAULT_BREADTH_MIN = 0.5
DEFAULT_RISK_PCT = 0.01
DEFAULT_MAX_WEIGHT = 0.10
DEFAULT_EXPOSURE_SCALE = 0.50


class StrategyError(Exception):
    """Base exception for strategy errors."""


class InvalidParameterError(StrategyError):
    """Raised when strategy parameters are invalid."""


class NotPreparedError(StrategyError):
    """Raised when strategy methods are called before prepare()."""


@dataclass(frozen=True)
class StrategyParams:
    """Immutable strategy parameters — makes illegal states unrepresentable."""

    lookback: int
    breadth_min: float
    risk_pct: float
    max_weight: float
    exposure_scale: float = DEFAULT_EXPOSURE_SCALE

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise InvalidParameterError(f"lookback must be >= 2, got {self.lookback}")
        if not 0.0 < self.breadth_min <= 1.0:
            raise InvalidParameterError(
                f"breadth_min must be in (0, 1], got {self.breadth_min}"
            )
        if not 0.0 < self.risk_pct <= 0.05:
            raise InvalidParameterError(
                f"risk_pct must be in (0, 0.05], got {self.risk_pct}"
            )
        if not 0.0 < self.max_weight <= 1.0:
            raise InvalidParameterError(
                f"max_weight must be in (0, 1], got {self.max_weight}"
            )
        if not 0.0 <= self.exposure_scale <= 1.0:
            raise InvalidParameterError(
                f"exposure_scale must be in [0, 1], got {self.exposure_scale}"
            )


@dataclass(frozen=True)
class OpenPosition:
    """Immutable open position record."""

    inst_id: str
    entry: float
    stop: float
    risk: float
    size: float
    signal_bar: int


class MyD1Strategy:
    """Unlocked D1 breakout with configurable parameters."""

    name = PLUGIN_NAME
    bar = "1Dutc"

    def __init__(
        self,
        allocated_capital: float,
        lookback: int = DEFAULT_LOOKBACK,
        breadth_min: float = DEFAULT_BREADTH_MIN,
        risk_pct: float = DEFAULT_RISK_PCT,
        max_weight: float = DEFAULT_MAX_WEIGHT,
        exposure_scale: float = DEFAULT_EXPOSURE_SCALE,
        exit_mode: str = "mid",
    ) -> None:
        if allocated_capital is None or allocated_capital <= 0:
            raise InvalidParameterError(
                "MyD1 requires allocated_capital > 0; never sizes from OKX account balance"
            )
        if abs(float(allocated_capital) - WEDDING_FUNDS_USDT) < 1e-9:
            raise InvalidParameterError(
                "allocated_capital equals the excluded 6,788 USDT wedding funds; refuse"
            )
        if exit_mode not in ("mid", "low"):
            raise InvalidParameterError(f"exit_mode must be 'mid' or 'low', got {exit_mode!r}")

        self.params = StrategyParams(
            lookback=lookback,
            breadth_min=breadth_min,
            risk_pct=risk_pct,
            max_weight=max_weight,
            exposure_scale=exposure_scale,
        )
        self.allocated_capital = float(allocated_capital)
        self.exit_mode = exit_mode

        # Populated by prepare()
        self.C: Optional[pd.DataFrame] = None
        self.HI: Optional[pd.DataFrame] = None
        self.LO: Optional[pd.DataFrame] = None
        self.U: Optional[pd.DataFrame] = None
        self.breadth: Optional[pd.Series] = None
        self.gate: Optional[pd.Series] = None
        self._ready: bool = False

    def warmup_bars(self) -> int:
        """Minimum bars needed before strategy can produce signals."""
        return 220  # SMA200 + lookback channel

    def prepare(
        self,
        close: pd.DataFrame,
        high: Optional[pd.DataFrame] = None,
        low: Optional[pd.DataFrame] = None,
        quote_vol: Optional[pd.DataFrame] = None,
        universe: Optional[pd.DataFrame] = None,
        breadth: Optional[pd.Series] = None,
    ) -> None:
        """Prepare strategy with panel data. Must be called before weights_and_trades."""
        if "BTC" not in close.columns:
            raise StrategyError("MyD1 needs a BTC column in the close panel")

        self.C = close.copy()
        self.HI = high.copy() if high is not None else close.copy()
        self.LO = low.copy() if low is not None else close.copy()
        self.U = (
            universe.copy()
            if universe is not None
            else monthly_top20_universe(
                close, quote_vol if quote_vol is not None else pd.DataFrame(0.0, close.index, close.columns)
            )
        )
        self.breadth = (
            breadth.copy()
            if breadth is not None
            else breadth_top20(close, self.U)
        )
        self.gate = (
            btc_above_sma200(close["BTC"])
            .astype(float)
            .reindex(close.index)
            .fillna(0.0)
        )
        self._ready = True
        logger.debug(
            "strategy prepared: symbols=%d bars=%d lookback=%d",
            len(self.C.columns),
            len(self.C),
            self.params.lookback,
        )

    def _require_ready(self) -> None:
        """EAFP: raise if prepare() hasn't been called."""
        if not self._ready:
            raise NotPreparedError("call prepare() before weights_and_trades()")

    def weights_and_trades(
        self, state: Optional[dict] = None
    ) -> tuple[pd.DataFrame, list[dict]]:
        """Return (weight DataFrame, trades list). Same shape as locked D1 plugin."""
        self._require_ready()

        if self.C is None or self.U is None or self.breadth is None or self.gate is None:
            raise StrategyError("Strategy data not initialized after prepare()")

        C = self.C
        U = self.U
        g = self.gate.values
        br = self.breadth.reindex(C.index).values
        w = pd.DataFrame(0.0, C.index, C.columns)
        trades: list[dict] = []

        for s in C.columns:
            c = C[s].values
            u = U[s].values
            prevmax = C[s].shift(1).rolling(self.params.lookback).max().values
            mx = C[s].rolling(self.params.lookback).max().values
            mn = C[s].rolling(self.params.lookback).min().values

            pos = False
            st = 0.0
            ep = 0.0
            risk = 0.0
            t0: Optional[int] = None
            size = 0.0
            col = np.zeros(len(c))

            for t in range(len(c)):
                if np.isnan(c[t]):
                    pos = False
                    continue

                stop_t = (mx[t] + mn[t]) / 2.0
                if pos:
                    if not np.isnan(stop_t):
                        st = max(st, stop_t)  # ratchet up only
                    if c[t] < st or g[t] == 0:
                        pos = False
                        if t0 is not None:
                            trades.append(
                                {
                                    "sym": s,
                                    "entry_idx": t0,
                                    "exit_idx": t,
                                    "entry": ep,
                                    "exit": float(c[t]),
                                    "risk": risk,
                                    "size": size,
                                }
                            )
                elif (
                    u[t]
                    and g[t] > 0
                    and br[t] >= self.params.breadth_min
                    and not np.isnan(prevmax[t])
                    and c[t] > prevmax[t]
                    and not np.isnan(stop_t)
                    and stop_t < c[t]
                ):
                    computed_risk = (c[t] - stop_t) / c[t]
                    # Chống chia cho 0 hoặc giá stop lỗi
                    if computed_risk <= 1e-6:
                        continue

                    pos = True
                    ep = float(c[t])
                    st = float(stop_t)
                    risk = computed_risk
                    t0 = t
                    size = min(self.params.risk_pct / risk, self.params.max_weight)

                col[t] = size if pos else 0.0

            w[s] = col
            if state is not None and pos and t0 is not None:
                state[s] = {
                    "entry": ep,
                    "stop": st,
                    "risk": risk,
                    "size": size,
                    "since": C.index[t0],
                    "new": t0 == len(c) - 1,
                    "close": float(c[-1]),
                }

        # cap_total: scale down if gross weight > 1
        ssum = w.sum(axis=1)
        w = w.div(np.maximum(ssum, 1.0), axis=0)
        gate_mask = pd.DataFrame(
            np.tile(g > 0, (len(C.columns), 1)).T,
            index=C.index,
            columns=C.columns,
        )
        w = w.mul(gate_mask, axis=0)
        w = w * self.params.exposure_scale

        logger.info(
            "weights computed: n_trades=%d gross_cap_hits=%d",
            len(trades),
            int((ssum > 1.0).sum()),
        )
        return w, trades

    def signals_at(
        self,
        ts: pd.Timestamp,
        weights_prev: dict[str, float],
        weights_now: dict[str, float],
        stops: dict[str, float],
    ) -> list[Signal]:
        """Translate weight changes into Signal objects."""
        sigs: list[Signal] = []
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
                        reason=f"my_d1 {self.params.lookback}-day close breakout",
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
                        reason="my_d1 ratchet mid-stop or BTC gate off",
                        strategy=self.name,
                    )
                )

        return sigs