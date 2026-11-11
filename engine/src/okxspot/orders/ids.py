from __future__ import annotations

import hashlib


def make_client_order_id(purpose: str, inst_id: str, side: str, bar_ts: int) -> str:
    """Deterministic OKX client id (alphanumeric, <= 32 chars).

    The same purpose, instrument, side, and bar produce the same id, so a
    retry or a restart cannot double-send that intent.
    """
    raw = f"{purpose}|{inst_id}|{side}|{bar_ts}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return "c" + digest[:31]
