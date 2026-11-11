"""Per-symbol/timeframe data-quality checks on cached candles."""

from __future__ import annotations

from typing import Iterable, Optional

from tradingbot.types import Candle

BAR_MS = {
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1Dutc": 86_400_000,
    "1D": 86_400_000,
    "1Mutc": None,  # irregular; skip gap grid
}


def assess(candles: Iterable[Candle], bar: str, inst_id: str = "") -> dict:
    rows = list(candles)
    interval = BAR_MS.get(bar)
    issues: list[str] = []
    warnings: list[str] = []
    n = len(rows)
    ts = [int(c.ts) for c in rows]
    closed = [c for c in rows if int(c.confirm) == 1]
    dup = 0
    non_mono = 0
    if ts:
        seen = {}
        for i, t in enumerate(ts):
            seen[t] = seen.get(t, 0) + 1
            if i > 0 and t <= ts[i - 1]:
                non_mono += 1
        dup = sum(v - 1 for v in seen.values() if v > 1)
    if dup:
        issues.append(f"duplicates:{dup}")
    if non_mono:
        issues.append(f"non_monotonic:{non_mono}")

    nan_ohlcv = 0
    zero_ohlc = 0
    zero_volume = 0
    for c in rows:
        vals = (c.open, c.high, c.low, c.close, c.volume_base, c.volume_quote)
        if any(v != v for v in vals):  # NaN
            nan_ohlcv += 1
        if any(v == 0 for v in (c.open, c.high, c.low, c.close)):
            zero_ohlc += 1
        if c.volume_base == 0 and c.volume_quote == 0:
            zero_volume += 1
    if nan_ohlcv:
        issues.append(f"nan_ohlcv:{nan_ohlcv}")
    if zero_ohlc:
        issues.append(f"zero_ohlc:{zero_ohlc}")
    if zero_volume:
        warnings.append(f"zero_volume:{zero_volume}")

    gaps = 0
    gap_ms_total = 0
    gap_examples: list[int] = []
    misaligned = 0
    # gap-check closed bars only, in sorted unique order
    closed_ts = sorted({int(c.ts) for c in closed})
    if interval and len(closed_ts) >= 2:
        for a, b in zip(closed_ts, closed_ts[1:]):
            if (a % interval) != 0:
                misaligned += 1
            delta = b - a
            if delta > interval:
                missing = int(delta // interval) - 1
                if missing > 0:
                    gaps += missing
                    gap_ms_total += missing * interval
                    if len(gap_examples) < 5:
                        gap_examples.append(a + interval)
        if closed_ts[-1] % interval != 0:
            misaligned += 1
    if gaps:
        issues.append(f"gaps:{gaps}")
    if misaligned:
        warnings.append(f"misaligned:{misaligned}")
    if n == 0:
        issues.append("empty")

    first_ts = ts[0] if ts else None
    last_ts = ts[-1] if ts else None
    return {
        "symbol": inst_id,
        "bar": bar,
        "n": n,
        "n_closed": len(closed),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duplicates": dup,
        "non_monotonic": non_mono,
        "gaps": gaps,
        "gap_ms_total": gap_ms_total,
        "gap_examples_ts": gap_examples,
        "nan_ohlcv": nan_ohlcv,
        "zero_ohlc": zero_ohlc,
        "zero_volume": zero_volume,
        "misaligned": misaligned,
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }
