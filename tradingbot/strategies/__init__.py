"""Spec v1 strategy plugins (V-A / V-B / V-C).

Kept in a separate package from ``tradingbot.strategy`` (live engine plugins)
so this evaluation can land without colliding with the concurrent paper-runner work.
"""

from tradingbot.strategies.spec_v1_common import SPEC_V1_PARAMS, VARIANT_IDS

__all__ = ["SPEC_V1_PARAMS", "VARIANT_IDS"]
