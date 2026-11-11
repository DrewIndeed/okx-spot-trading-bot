"""V-B ENS-LF: TSMOM 21/63/252 + Donchian 20/60/90, long iff ≥4/6, vol 12%, F2+F1. Frozen."""

from tradingbot.strategies.spec_v1_common import VARIANT_NAMES
from tradingbot.strategies.spec_v1_signals import vb_signals

VARIANT_ID = "V-B"
VARIANT_NAME = VARIANT_NAMES[VARIANT_ID]
REGIME = "F2 HALT + F1 REDUCE (Macroy V2). Vol target 12%. 20% rebalance band."

__all__ = ["VARIANT_ID", "VARIANT_NAME", "REGIME", "vb_signals"]
