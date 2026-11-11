"""SHA256 manifest for the v3 candle cache. Cache files themselves are gitignored."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from tradingbot.data.store import COLUMNS, existing_cache_file, load_candles

BARS = ("1Dutc", "4H", "1H")


def file_sha256(path: Path | str) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _inst_bar_from_name(path: Path) -> tuple[str, str]:
    stem = path.stem  # e.g. BTC-USDT_1Dutc
    for bar in ("1Dutc", "4H", "1H"):
        suffix = f"_{bar}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)], bar
    return stem, ""


def series_record(path: Path) -> dict[str, Any]:
    inst, bar = _inst_bar_from_name(path)
    candles = load_candles(path.parent, inst, bar) if inst and bar else []
    closed = [c for c in candles if int(c.confirm) == 1]
    first = min((c.ts for c in closed), default=None)
    last = max((c.ts for c in closed), default=None)
    return {
        "path": path.name,
        "inst_id": inst,
        "bar": bar,
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "n": len(candles),
        "n_closed": len(closed),
        "first_ts": first,
        "last_ts": last,
        "first_utc": (
            datetime.fromtimestamp(first / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if first
            else None
        ),
        "last_utc": (
            datetime.fromtimestamp(last / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if last
            else None
        ),
    }


def iter_cache_files(cache_dir: Path | str, bars: tuple[str, ...] = BARS) -> list[Path]:
    cache_dir = Path(cache_dir)
    out: list[Path] = []
    if not cache_dir.exists():
        return out
    for p in sorted(cache_dir.iterdir()):
        if p.suffix not in {".parquet", ".csv"}:
            continue
        if p.name.endswith("_meta.json") or p.name.startswith("sync_"):
            continue
        inst, bar = _inst_bar_from_name(p)
        if bar in bars:
            out.append(p)
    return out


def build_manifest(
    cache_dir: Path | str,
    bars: tuple[str, ...] = BARS,
    extra: dict | None = None,
) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    files = iter_cache_files(cache_dir, bars=bars)
    series = [series_record(p) for p in files]
    insts = sorted({s["inst_id"] for s in series if s["inst_id"]})
    payload: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_dir": str(cache_dir.resolve()),
        "bars": list(bars),
        "n_files": len(series),
        "n_instruments": len(insts),
        "instruments": insts,
        "series": series,
    }
    if extra:
        payload["extra"] = extra
    return payload


def write_manifest(manifest: dict, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return path


def load_manifest(path: Path | str) -> dict:
    return json.loads(Path(path).read_text())


def verify_manifest(manifest: dict, cache_dir: Path | str) -> list[str]:
    """Return a list of mismatch descriptions. Empty means OK."""
    cache_dir = Path(cache_dir)
    problems: list[str] = []
    seen = set()
    for rec in manifest.get("series") or []:
        p = cache_dir / rec["path"]
        seen.add(p.name)
        if not p.exists():
            problems.append(f"missing {rec['path']}")
            continue
        got = file_sha256(p)
        if got != rec["sha256"]:
            problems.append(f"checksum mismatch {rec['path']}: manifest={rec['sha256']} disk={got}")
    return problems
