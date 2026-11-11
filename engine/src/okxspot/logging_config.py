from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


class Log:
    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def info(self, msg: str, **fields: object) -> None:
        self._logger.info(msg, extra={"fields": fields})

    def warning(self, msg: str, **fields: object) -> None:
        self._logger.warning(msg, extra={"fields": fields})

    def error(self, msg: str, **fields: object) -> None:
        self._logger.error(msg, extra={"fields": fields})
