"""Causal indicators reused from /workspace/crypto/ind.py (EMA SMA-seeded, Wilder ATR)."""

from __future__ import annotations

import numpy as np


def ema(x, n: int):
    """EMA with SMA seed at index n-1, then recursive. Causal: value[i] uses data <= i only.

    Reused from /workspace/crypto/ind.py.
    """
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def wilder(x, n: int):
    """Wilder smoothing (RMA), seeded with SMA of first n values. Reused from /workspace/crypto/ind.py."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def true_range(h, l, c):
    """Reused from /workspace/crypto/ind.py."""
    pc = np.concatenate([[c[0]], c[:-1]])
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def atr(h, l, c, n: int = 14):
    """Wilder ATR. Reused from /workspace/crypto/ind.py."""
    return wilder(true_range(h, l, c), n)
