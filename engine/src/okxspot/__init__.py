"""Spot-only OKX trading engine.

No strategy in this package has been shown to be profitable. Live trading is
off unless the operator passes an explicit CLI flag and a confirmation env var.
"""

from __future__ import annotations

__version__ = "0.1.0"

DISCLAIMER = (
    "Research output only. No strategy in this package has been shown to be "
    "profitable. Backtests are not evidence of future results."
)
