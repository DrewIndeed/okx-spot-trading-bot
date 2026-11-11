"""Telegram alert hook. T1 stub: never sends a network message (no external send without Andrew).

If TELEGRAM_BOT_TOKEN is absent, we only log that alerts are disabled.
If a token is present, we still do not send — we log a stub event and never print the token.
"""

from __future__ import annotations

import os
from typing import Optional

from tradingbot.logutil import JsonlLogger


def send_alert(text: str, logger: Optional[JsonlLogger] = None, token: Optional[str] = None) -> bool:
    """Return True only if a real send happened. T1 always returns False."""
    log = logger or JsonlLogger()
    # Read token only to decide the branch; never log or return it.
    has_token = bool((token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip())
    if not has_token:
        log.info("telegram_disabled", reason="no token", text=text)
        return False
    log.info(
        "telegram_stub_not_sent",
        reason="scaffold_no_external_send",
        has_token=True,
        text=text,
    )
    return False
