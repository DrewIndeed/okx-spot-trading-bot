from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from okxspot.config import Settings

log = logging.getLogger("okxspot.alert")


class Alerter(Protocol):
    def send(self, level: str, message: str) -> None:
        """Deliver one alert. Implementations must not raise."""


class LogAlerter:
    def send(self, level: str, message: str) -> None:
        log.info("%s %s", level, message)


class TelegramAlerter:
    """Optional alerter. Token is read from the environment, never from YAML."""

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=10)
        self._owns_client = client is None

    def send(self, level: str, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            response = self._client.post(
                url,
                json={"chat_id": self._chat_id, "text": f"[{level}] {message}"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            log.error("telegram delivery failed")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def build_alerter(settings: Settings) -> Alerter:
    if not settings.telegram.enabled:
        return LogAlerter()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return LogAlerter()
    return TelegramAlerter(token, chat_id)
