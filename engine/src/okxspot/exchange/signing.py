from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import urlencode


def okx_sign(secret: str, timestamp: str, method: str, path: str, body: str) -> str:
    """OKX v5 prehash: ``timestamp + METHOD + requestPath + body``, HMAC-SHA256, base64."""
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_request_path(path: str, params: dict[str, str] | None) -> str:
    if not params:
        return path
    query = urlencode(list(params.items()))
    return f"{path}?{query}"


def format_okx_timestamp(epoch_ms: int) -> str:
    if epoch_ms < 0:
        raise ValueError("timestamp must be non-negative")
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    millis = epoch_ms % 1000
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def clock_offset_ms(local_ms: int, server_ms: int) -> int:
    return server_ms - local_ms
