"""V-A D1-BASE: production 20-day close breakout + F2 HALT + R1 hard gate. Frozen."""

from tradingbot.strategies.spec_v1_common import VARIANT_NAMES
from tradingbot.strategies.spec_v1_signals import va_indicators

VARIANT_ID = "V-A"
VARIANT_NAME = VARIANT_NAMES[VARIANT_ID]
REGIME = "F2 HALT + production R1 (F1-as-block). No F3/F4. No vol target."

__all__ = ["VARIANT_ID", "VARIANT_NAME", "REGIME", "va_indicators"]
