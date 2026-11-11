"""Frozen, pre-registered parameters for strategy-spec v1. Do not tune on OOS/holdout."""

from __future__ import annotations

from typing import Final

# Capital is NEVER inferred from the OKX main account or from 6,788 USDT (wedding funds).
WEDDING_FUNDS_USDT: Final[float] = 6788.0  # excluded; documented only, never a default

VARIANT_IDS: Final[tuple[str, ...]] = ("V-A", "V-B", "V-C")
VARIANT_NAMES: Final[dict[str, str]] = {
    "V-A": "D1-BASE",
    "V-B": "ENS-LF",
    "V-C": "ZARA-DK",
}

# Cost per side (all-in) as fractions.
COST_LEVELS: Final[dict[str, float]] = {
    "1x": 0.0020,       # 20 bps: 0.10% fee + 0.10% slip
    "25bps": 0.0025,    # Lisaac 25 bps combined one-way stress
    "2x": 0.0040,       # 40 bps: 0.20% fee + 0.20% slip (PRIMARY)
    "50bps": 0.0050,    # Lisaac 50 bps combined one-way stress
}
PRIMARY_COST: Final[str] = "2x"

# Shared risk (spec §4.3)
GROSS_CAP: Final[float] = 1.0
NAME_CAP: Final[float] = 0.10
RISK_PCT: Final[float] = 0.01
VOL_TARGET: Final[float] = 0.12
VOL_LOOKBACK: Final[int] = 90
VOL_MIN_PERIODS: Final[int] = 60
VOL_ANN_FACTOR: Final[float] = 365.0 ** 0.5
VOL_BAND: Final[float] = 0.20
DAILY_LOSS_CAP: Final[float] = -0.02
KILL_SWITCH_DD: Final[float] = -0.15
BREADTH_MIN: Final[float] = 0.50

# V-A
VA_BREAKOUT_N: Final[int] = 20
VA_STOP_N: Final[int] = 20
VA_SMA_BTC: Final[int] = 200
VA_SMA_BREADTH: Final[int] = 50

# V-B
VB_TSMOM: Final[tuple[int, ...]] = (21, 63, 252)
VB_DONCHIAN: Final[tuple[int, ...]] = (20, 60, 90)
VB_MIN_COMPONENTS: Final[int] = 4
VB_N_COMPONENTS: Final[int] = 6

# V-C
VC_DONCHIAN: Final[tuple[int, ...]] = (20, 60, 90)
VC_SLEEVE_VOL: Final[float] = VOL_TARGET / 3.0  # 1/3 of 12% vol budget

# F1 / F2
F1_SMA: Final[int] = 200
F1_BTC_STALE_DAYS: Final[int] = 2
F2_WINDOW_HOURS: Final[int] = 24
F2_CALENDAR_STALE_DAYS: Final[int] = 7
F2_EVENT_TYPES: Final[tuple[str, ...]] = ("CPI", "PPI", "FOMC_STATEMENT")

# Protocol windows (UTC calendar dates, inclusive)
WARMUP_END: Final[str] = "2022-12-31"
FOLDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("F1", "2023-01-01", "2023-06-30"),
    ("F2", "2023-07-01", "2023-12-31"),
    ("F3", "2024-01-01", "2024-06-30"),
    ("F4", "2024-07-01", "2024-12-31"),
)
POOLED_OOS: Final[tuple[str, str]] = ("2023-01-01", "2024-12-31")
BUFFER: Final[tuple[str, str]] = ("2025-01-01", "2025-03-19")
HOLDOUT_H1: Final[tuple[str, str]] = ("2025-03-20", "2025-11-02")
HOLDOUT_H2_START: Final[str] = "2025-11-03"  # end = last closed 1Dutc

# Acceptance floors
FOLD_MIN_DAYS: Final[int] = 60
FOLD_MIN_TRADES: Final[int] = 10
FOLD_MAJORITY: Final[int] = 3
POOLED_MIN_TRADES: Final[int] = 30
POOLED_MIN_SHARPE: Final[float] = 0.50
MAXDD_FLOOR: Final[float] = -0.20  # must be strictly greater (shallower)
HOLDOUT_MIN_TRADES: Final[int] = 15
DSR_N_TRIALS: Final[int] = 3
DSR_N_CONSERVATIVE: Final[int] = 38
DSR_MIN: Final[float] = 0.95
RESEARCH4_PBO: Final[float] = 0.429  # context only, not recomputed

# Evaluable-window / reporting
PATH_START: Final[str] = "2023-01-01"  # first OOS day; equity starts at 1.0 * C

SPEC_V1_PARAMS: Final[dict] = {
    "spec": "strategy-spec v1",
    "variants": dict(VARIANT_NAMES),
    "cost_levels": dict(COST_LEVELS),
    "primary_cost": PRIMARY_COST,
    "gross_cap": GROSS_CAP,
    "name_cap": NAME_CAP,
    "risk_pct": RISK_PCT,
    "vol_target": VOL_TARGET,
    "vol_lookback": VOL_LOOKBACK,
    "vol_min_periods": VOL_MIN_PERIODS,
    "vol_ann_factor": "sqrt(365)",
    "vol_band": VOL_BAND,
    "daily_loss_cap": DAILY_LOSS_CAP,
    "kill_switch_dd": KILL_SWITCH_DD,
    "breadth_min": BREADTH_MIN,
    "va": {"breakout_n": VA_BREAKOUT_N, "stop_n": VA_STOP_N, "sma_btc": VA_SMA_BTC, "sma_breadth": VA_SMA_BREADTH},
    "vb": {"tsmom": VB_TSMOM, "donchian": VB_DONCHIAN, "min_components": VB_MIN_COMPONENTS},
    "vc": {"donchian": VC_DONCHIAN, "sleeve_vol": VC_SLEEVE_VOL},
    "f1_sma": F1_SMA,
    "f2_window_hours": F2_WINDOW_HOURS,
    "folds": FOLDS,
    "pooled_oos": POOLED_OOS,
    "buffer": BUFFER,
    "holdout_h1": HOLDOUT_H1,
    "holdout_h2_start": HOLDOUT_H2_START,
    "dsr_n_trials": DSR_N_TRIALS,
    "dsr_n_conservative": DSR_N_CONSERVATIVE,
    "survivorship": "Restricted to the current favorites.txt list; delisted/absent coins are not reconstructed.",
}
