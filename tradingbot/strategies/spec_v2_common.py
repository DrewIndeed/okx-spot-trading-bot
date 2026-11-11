"""Frozen, pre-registered parameters for strategy-spec v2. Do not tune on forward data."""

from __future__ import annotations

from typing import Final

WEDDING_FUNDS_USDT: Final[float] = 6788.0  # excluded; never a default for C_paper

SPEC_V2_PATH: Final[str] = "/workspace/sprint/strategy-spec-v2.md"
T0_CLOSE_UTC: Final[str] = "2026-11-12T00:00:00Z"  # 2026-11-12 07:00 UTC+7
T0_BAR_OPEN_UTC: Final[str] = "2026-11-11T00:00:00Z"  # 1Dutc open whose close is T0
OFFICIAL_RUN_ID: Final[str] = "spec_v2_official"

# Shared cost: 2× = 40 bps/side (0.20% fee + 0.20% slip), same as spec v1 primary.
COST_2X: Final[float] = 0.0040

# V2-CORE
CORE_UNIVERSE: Final[tuple[str, ...]] = ("BTC", "ETH")
CORE_SMA: Final[int] = 200
CORE_DONCHIAN: Final[tuple[int, ...]] = (20, 60, 90)
CORE_MIN_DONCHIAN: Final[int] = 2
CORE_VOL_TARGET: Final[float] = 0.12
CORE_VOL_LOOKBACK: Final[int] = 90
CORE_VOL_MIN_PERIODS: Final[int] = 60
CORE_VOL_ANN_FACTOR: Final[float] = 365.0 ** 0.5
CORE_GROSS_CAP: Final[float] = 1.0  # no leverage
CORE_DAILY_LOSS_CAP: Final[float] = -0.02
CORE_KILL_DD: Final[float] = -0.15
CORE_REARM_FLAT_DAYS: Final[int] = 30  # closed 1Dutc sessions all-flat, then BTC > SMA200

# Benchmark: 50/50 BTC/ETH, monthly rebalance, same 2x fees.
BENCH_WEIGHT: Final[float] = 0.50
BENCH_REBALANCE: Final[str] = "monthly"  # first 1Dutc bar of each UTC calendar month

# V2-ZARA-FROZEN is V-C unchanged; hashes recorded at freeze time from spec v1 lock.
ZARA_VARIANT_ID: Final[str] = "V-C"
ZARA_NAME: Final[str] = "V2-ZARA-FROZEN"

SPEC_V2_CORE_PARAMS: Final[dict] = {
    "spec": "strategy-spec v2",
    "variant": "V2-CORE",
    "universe": list(CORE_UNIVERSE),
    "entry": "close > SMA200 AND >=2 of 3 Donchian(20/60/90) long",
    "donchian": CORE_DONCHIAN,
    "min_donchian": CORE_MIN_DONCHIAN,
    "sma": CORE_SMA,
    "vol_target": CORE_VOL_TARGET,
    "vol_lookback": CORE_VOL_LOOKBACK,
    "vol_min_periods": CORE_VOL_MIN_PERIODS,
    "vol_ann": "log close-to-close * sqrt(365)",
    "gross_cap": CORE_GROSS_CAP,
    "leverage": False,
    "name_cap": None,  # not in v2; 10% v1 cap would break a 2-asset 12% vol book
    "rebalance_band": None,  # not in v2; trade to vol-target each closed bar
    "regime": "F2 HALT +/-24h CPI/PPI/FOMC_STATEMENT + F1 REDUCE (BTC close <= SMA200)",
    "daily_loss_cap": CORE_DAILY_LOSS_CAP,
    "kill_switch_dd": CORE_KILL_DD,
    "kill_rearm": "non-sticky: re-arm after 30 closed 1Dutc days flat AND BTC > SMA200; peak reset to equity on re-arm",
    "cost_per_side": COST_2X,
    "fill": "signal on closed 1Dutc t (confirm==1); fill at open of t+1",
    "t0_close_utc": T0_CLOSE_UTC,
    "benchmark": "50/50 BTC/ETH buy-and-hold, monthly rebalance, 2x fees",
    "capital": "C_paper required config; never OKX balance; 6788 USDT wedding funds excluded",
}
