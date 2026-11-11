from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from okxspot.decimals import dec_str, parse_decimal
from okxspot.exchange.okx_parse import parse_okx_candle_row
from okxspot.models import Candle


class HistorySource(Protocol):
    def fetch_history_page(
        self,
        inst_id: str,
        bar: str,
        after_ms: int | None,
        limit: int,
    ) -> list[list[str]]:
        """Return OKX rows with timestamps strictly earlier than ``after_ms`` when set."""


def cache_path(directory: Path, inst_id: str, bar: str) -> Path:
    safe = inst_id.replace("/", "_")
    return directory / f"{safe}_{bar}.jsonl"


def load_cache(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    candles: list[Candle] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            continue
        candles.append(
            Candle(
                inst_id=str(raw["inst_id"]),
                ts=datetime.fromisoformat(str(raw["ts"])),
                open=parse_decimal(raw["open"]),
                high=parse_decimal(raw["high"]),
                low=parse_decimal(raw["low"]),
                close=parse_decimal(raw["close"]),
                volume=parse_decimal(raw["volume"]),
                confirmed=bool(raw.get("confirmed", True)),
            )
        )
    candles.sort(key=lambda candle: candle.ts)
    return candles


def save_cache(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for candle in sorted(candles, key=lambda item: item.ts):
        lines.append(
            json.dumps(
                {
                    "inst_id": candle.inst_id,
                    "ts": candle.ts.isoformat(),
                    "open": dec_str(candle.open),
                    "high": dec_str(candle.high),
                    "low": dec_str(candle.low),
                    "close": dec_str(candle.close),
                    "volume": dec_str(candle.volume),
                    "confirmed": candle.confirmed,
                }
            )
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _covers(candles: list[Candle], start: datetime, end: datetime) -> bool:
    if not candles:
        return False
    return candles[0].ts <= start and candles[-1].ts >= end


def download_candles(
    source: HistorySource,
    inst_id: str,
    bar: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path | None = None,
    limit: int = 100,
) -> list[Candle]:
    """Download confirmed candles in ``[start, end]`` and cache them.

    Pagination follows OKX ``history-candles``: ``after`` returns rows older
    than that millisecond timestamp, newest first.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    path = cache_path(cache_dir, inst_id, bar) if cache_dir is not None else None
    cached = load_cache(path) if path is not None else []
    if _covers(cached, start, end):
        return [candle for candle in cached if start <= candle.ts <= end and candle.confirmed]
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    found = {int(candle.ts.timestamp() * 1000): candle for candle in cached}
    cursor = end_ms + 1
    while True:
        rows = source.fetch_history_page(inst_id, bar, cursor, limit)
        if not rows:
            break
        page = [parse_okx_candle_row(inst_id, row) for row in rows]
        oldest = min(int(candle.ts.timestamp() * 1000) for candle in page)
        for candle in page:
            ms = int(candle.ts.timestamp() * 1000)
            if start_ms <= ms <= end_ms and candle.confirmed:
                found[ms] = candle
        if oldest <= start_ms or oldest >= cursor:
            break
        cursor = oldest
    ordered = [found[key] for key in sorted(found)]
    if path is not None:
        save_cache(path, ordered)
    return [candle for candle in ordered if start <= candle.ts <= end]


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)
