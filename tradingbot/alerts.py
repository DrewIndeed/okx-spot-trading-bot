"""Telegram alert hook.

Real send ONLY when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
Never logs or echoes the token. Alerts are messages only — no orders, no keys.
"""

from __future__ import annotations

import os
from typing import Optional

from tradingbot.logutil import JsonlLogger


def _post(token: str, chat_id: str, text: str) -> bool:
    import json as _json
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = _json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        # Never surface the exception: urllib errors can carry the request URL (with token).
        return False
    return ok


def send_alert(text: str, logger: Optional[JsonlLogger] = None, token: Optional[str] = None) -> bool:
    """Return True only if a real send happened. Text is truncated to 4000 chars."""
    log = logger or JsonlLogger()
    tok = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not tok or not chat:
        log.info("telegram_disabled", reason="missing token or chat id", text=text)
        return False
    sent = _post(tok, chat, text[:4000])
    if sent:
        log.info("telegram_sent", chat_id=chat, chars=len(text))
    else:
        log.info("telegram_send_failed", chat_id=chat, text=text)
    return sent
