from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from okxspot.config import Settings
from okxspot.redact import register_secret

log = logging.getLogger("okxspot.alert")


class Alerter(Protocol):
    def send(self, level: str, message: str, event: str | None = None) -> None:
        """Deliver one alert. Implementations must not raise."""


class LogAlerter:
    def send(self, level: str, message: str, event: str | None = None) -> None:
        suffix = f" event={event}" if event else ""
        log.info("%s %s%s", level, message, suffix)


class FileAlerter:
    """Default durable sink. Never writes secrets."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, level: str, message: str, event: str | None = None) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "level": level,
            "event": event or "log",
            "message": message,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            log.error("file alerter failed")


class TelegramAlerter:
    """Optional alerter. Token is read from the environment, never from YAML."""

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=10)
        self._owns_client = client is None
        register_secret(token)

    def send(self, level: str, message: str, event: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        text = f"[{level}] {message}" if not event else f"[{level}/{event}] {message}"
        try:
            response = self._client.post(
                url,
                json={"chat_id": self._chat_id, "text": text},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("telegram delivery failed: %s", type(exc).__name__)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class CompositeAlerter:
    def __init__(self, sinks: list[Alerter]) -> None:
        self._sinks = sinks

    def send(self, level: str, message: str, event: str | None = None) -> None:
        for sink in self._sinks:
            try:
                sink.send(level, message, event)
            except TypeError:
                sink.send(level, message)
            except Exception:
                log.error("alerter sink failed")


@dataclass
class AlertEvent:
    level: str
    message: str
    event: str | None


class MemoryAlerter:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def send(self, level: str, message: str, event: str | None = None) -> None:
        self.events.append(AlertEvent(level, message, event))


def build_alerter(settings: Settings) -> Alerter:
    sinks: list[Alerter] = [LogAlerter()]
    file_path = getattr(getattr(settings, "alerts", None), "file_path", None)
    if file_path:
        sinks.append(FileAlerter(file_path))
    if settings.telegram.enabled:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.warning("telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        else:
            sinks.append(TelegramAlerter(token, chat_id))
    if len(sinks) == 1:
        return sinks[0]
    return CompositeAlerter(sinks)
