"""On-disk candle cache. Prefers parquet (pyarrow); falls back to CSV if pyarrow is missing.

Also reads legacy JSON written by the first T1 scaffold. We never write into /workspace/crypto.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from tradingbot.types import Candle

COLUMNS = ["ts", "open", "high", "low", "close", "volume_base", "volume_quote", "confirm"]


def cache_backend() -> str:
    try:
        import pyarrow  # noqa: F401
        return "parquet"
    except ImportError:
        return "csv"


BACKEND = cache_backend()


def _safe(inst_id: str) -> str:
    return inst_id.replace("/", "-")


def cache_stem(cache_dir: Path | str, inst_id: str, bar: str) -> Path:
    return Path(cache_dir) / f"{_safe(inst_id)}_{bar}"


def existing_cache_file(cache_dir: Path | str, inst_id: str, bar: str) -> Optional[Path]:
    stem = cache_stem(cache_dir, inst_id, bar)
    for ext in (".parquet", ".csv", ".json"):
        p = stem.with_suffix(ext)
        if p.exists():
            return p
    return None


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    rows = [c.as_row() for c in candles]
    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df["ts"] = df["ts"].astype("int64")
        df["confirm"] = df["confirm"].astype("int8")
    return df


def frame_to_candles(df: pd.DataFrame) -> list[Candle]:
    if df is None or df.empty:
        return []
    out = []
    for rec in df.to_dict(orient="records"):
        out.append(
            Candle(
                ts=int(rec["ts"]),
                open=float(rec["open"]),
                high=float(rec["high"]),
                low=float(rec["low"]),
                close=float(rec["close"]),
                volume_base=float(rec.get("volume_base") or 0.0),
                volume_quote=float(rec.get("volume_quote") or 0.0),
                confirm=int(1 if rec.get("confirm") is None else rec["confirm"]),
            )
        )
    return out


def load_candles(cache_dir: Path | str, inst_id: str, bar: str) -> list[Candle]:
    p = existing_cache_file(cache_dir, inst_id, bar)
    if p is None:
        return []
    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    elif p.suffix == ".csv":
        df = pd.read_csv(p)
    elif p.suffix == ".json":
        rows = json.loads(p.read_text())
        return [Candle.from_row(r) for r in rows]
    else:
        return []
    # tolerate column order
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = 0 if col != "confirm" else 1
    return frame_to_candles(df[COLUMNS])


def save_candles(
    cache_dir: Path | str,
    inst_id: str,
    bar: str,
    candles: list[Candle],
    *,
    backend: Optional[str] = None,
) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    fmt = backend or BACKEND
    stem = cache_stem(cache_dir, inst_id, bar)
    df = candles_to_frame(candles)
    if fmt == "parquet":
        path = stem.with_suffix(".parquet")
        try:
            df.to_parquet(path, index=False, engine="pyarrow")
            return path
        except Exception:
            fmt = "csv"
    path = stem.with_suffix(".csv")
    df.to_csv(path, index=False)
    return path
