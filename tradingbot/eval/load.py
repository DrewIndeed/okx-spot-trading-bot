"""Load closed 1Dutc candles from the local cache (read-only). Never hits private APIs."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from tradingbot.universe import load_favorites

TB_CACHE = Path("/workspace/trading-bot/data/cache")
CRYPTO_CACHE = Path("/workspace/crypto/data")


def _cache_paths(base: str) -> list[Path]:
    # Prefer the trading-bot cache; seed from /workspace/crypto if needed (read-only).
    return [
        TB_CACHE / f"{base}-USDT_1Dutc.json",
        CRYPTO_CACHE / f"{base}_1Dutc.json",
    ]


def load_closed_1dutc(base: str) -> pd.DataFrame:
    """Return a DataFrame indexed by UTC open date with o/h/l/c/vq, confirm==1 only."""
    path = next((p for p in _cache_paths(base) if p.exists()), None)
    if path is None:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vq"])
    rows = json.loads(path.read_text())
    recs = []
    for r in rows:
        if len(r) < 8:
            continue
        if int(r[7]) != 1:
            continue  # open bar rejected
        ts = int(r[0])
        recs.append(
            {
                "ts": ts,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "vq": float(r[6]),
            }
        )
    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vq"])
    df = pd.DataFrame(recs)
    idx = pd.to_datetime(df["ts"], unit="ms", utc=True)
    idx = pd.DatetimeIndex(idx).tz_convert("UTC").tz_localize(None).normalize()
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["open", "high", "low", "close", "vq"]]


def load_panel(favorites: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Load all favorites into aligned close/high/low/open/vq frames (NaN if a bar is missing)."""
    bases = favorites or load_favorites()
    frames: dict[str, pd.DataFrame] = {}
    for b in bases:
        df = load_closed_1dutc(b)
        if not df.empty:
            frames[b] = df
    if not frames:
        raise RuntimeError("No 1Dutc caches found for the favorites universe.")
    # Master calendar: union of all closed dates (crypto trades 24/7). BTC is required for F1/R1.
    if "BTC" not in frames:
        raise RuntimeError("BTC-USDT closed 1Dutc cache is required for F1/R1 and is missing.")
    idx = frames["BTC"].index
    for df in frames.values():
        idx = idx.union(df.index)
    idx = idx.sort_values()

    def _align(field: str) -> pd.DataFrame:
        data = {b: frames[b][field] for b in frames}
        out = pd.DataFrame(data, index=idx)
        return out.sort_index()

    return {
        "open": _align("open"),
        "high": _align("high"),
        "low": _align("low"),
        "close": _align("close"),
        "vq": _align("vq"),
        "bases": list(frames.keys()),
        "last_closed": idx[-1],
    }
