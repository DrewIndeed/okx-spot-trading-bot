from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class OrderJournal:
    """Append-only JSONL journal of order intents and exchange observations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **{key: _jsonable(value) for key, value in fields.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        return rows


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        try:
            return value.value
        except Exception:
            pass
    if hasattr(value, "__str__") and not isinstance(value, (str, int, float, bool, list, dict, type(None))):
        return str(value)
    return value
