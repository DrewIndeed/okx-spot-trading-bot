"""V-C ZARA-DK: Donchian 20/60/90 equal sleeves, vol 12%, F2 only. Frozen."""

from tradingbot.strategies.spec_v1_common import VARIANT_NAMES
from tradingbot.strategies.spec_v1_signals import vc_signals

VARIANT_ID = "V-C"
VARIANT_NAME = VARIANT_NAMES[VARIANT_ID]
REGIME = "F2 HALT only. Three equal sleeves, each 1/3 of the 12% vol budget. 20% band."

__all__ = ["VARIANT_ID", "VARIANT_NAME", "REGIME", "vc_signals"]
