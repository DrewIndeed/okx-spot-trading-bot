"""Load closed candles from the v3 cache (parquet preferred). Public cache only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tradingbot.data.store import load_candles
from tradingbot.data.v3_universe import seed_bases
from tradingbot.universe import base_sym, inst_id

DEFAULT_CACHE = Path("/workspace/trading-bot-v3/data/cache")


def load_closed(inst: str, bar: str = "1Dutc", cache_dir: Path | str = DEFAULT_CACHE) -> pd.DataFrame:
    """DataFrame indexed by UTC open (tz-naive, 1Dutc normalized to date) with o/h/l/c/vq, confirm==1 only."""
    inst = inst_id(inst)
    candles = [c for c in load_candles(cache_dir, inst, bar) if int(c.confirm) == 1]
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vq"])
    recs = [
        {
            "ts": c.ts,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "vq": float(c.volume_quote),
        }
        for c in candles
    ]
    df = pd.DataFrame(recs)
    idx = pd.to_datetime(df["ts"], unit="ms", utc=True)
    idx = pd.DatetimeIndex(idx).tz_convert("UTC").tz_localize(None)
    if bar == "1Dutc":
        idx = idx.normalize()
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["open", "high", "low", "close", "vq"]]


def load_panel(
    cache_dir: Path | str = DEFAULT_CACHE,
    bases: list[str] | None = None,
    bar: str = "1Dutc",
) -> dict:
    """Aligned close/high/low/open/vq frames. BTC is required."""
    cache_dir = Path(cache_dir)
    names = bases if bases is not None else seed_bases()
    frames: dict[str, pd.DataFrame] = {}
    for b in names:
        df = load_closed(b, bar=bar, cache_dir=cache_dir)
        if not df.empty:
            frames[base_sym(inst_id(b))] = df
    if not frames:
        raise RuntimeError(f"No {bar} caches found in {cache_dir}")
    if "BTC" not in frames:
        raise RuntimeError("BTC-USDT closed cache is required and is missing.")
    idx = frames["BTC"].index
    for df in frames.values():
        idx = idx.union(df.index)
    idx = idx.sort_values()

    def _align(field: str) -> pd.DataFrame:
        return pd.DataFrame({b: frames[b][field] for b in frames}, index=idx).sort_index()

    return {
        "open": _align("open"),
        "high": _align("high"),
        "low": _align("low"),
        "close": _align("close"),
        "vq": _align("vq"),
        "bases": list(frames.keys()),
        "last_closed": idx[-1],
        "cache_dir": str(cache_dir),
        "bar": bar,
    }
