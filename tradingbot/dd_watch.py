"""Paper drawdown watch: log each crossing of -15% and -20% from the paper peak.

Informational only. The book is not killed here; the kill switch stays at
risk.max_drawdown (0.30 for my_d1 paper). A level re-arms once drawdown recovers
above it, so every new crossing is logged exactly once. State lives in a small
JSON sidecar next to the portfolio state file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

WARN_LEVELS = (0.15, 0.20)


def sidecar_path(state_path: str | Path) -> Path:
    return Path(state_path).parent / "dd_breaches.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check_breaches(
    equity: float,
    peak: float,
    *,
    day: str,
    state_path: str | Path,
    levels: tuple[float, ...] = WARN_LEVELS,
) -> tuple[float, list[dict]]:
    """Return (drawdown, new breach events) and persist which levels are currently crossed."""
    dd = (peak - equity) / peak if peak > 0 else 0.0
    path = sidecar_path(state_path)
    data = _load(path)
    crossed = {str(k): bool(v) for k, v in (data.get("crossed") or {}).items()}
    events: list[dict] = []
    for lvl in levels:
        key = f"{lvl:.2f}"
        now = dd >= lvl
        if now and not crossed.get(key, False):
            events.append({"day": day, "level": -lvl, "drawdown": -dd, "equity": equity, "peak": peak})
        crossed[key] = now
    history = list(data.get("events") or []) + events
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"crossed": crossed, "events": history[-500:]}, indent=2))
    return dd, events


def describe(events: list[dict]) -> Optional[str]:
    if not events:
        return None
    parts = [f"{100 * e['level']:.0f}% (dd {100 * e['drawdown']:.1f}%)" for e in events]
    return "drawdown breach " + ", ".join(parts) + "; not killed (kill switch at max_drawdown)"
