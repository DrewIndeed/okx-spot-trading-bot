"""Structured JSONL logging. Never logs secrets (token/key/secret/passphrase/password)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_SECRET_KEYS = re.compile(r"(token|secret|passphrase|password|api[_-]?key|private[_-]?key|authorization)", re.I)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(x) for x in obj]
    return obj


class JsonlLogger:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, level: str, event: str, **fields: Any) -> dict:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "level": level,
            "event": event,
            **_redact(fields),
        }
        line = json.dumps(rec, default=str)
        if self.path:
            with self.path.open("a") as fh:
                fh.write(line + "\n")
        return rec

    def info(self, event: str, **fields: Any) -> dict:
        return self.emit("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> dict:
        return self.emit("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> dict:
        return self.emit("error", event, **fields)
