from __future__ import annotations

import hashlib


def make_client_order_id(
    strategy: str,
    inst_id: str,
    side: str,
    bar_ts: int,
    purpose: str = "entry",
) -> str:
    """Deterministic OKX client id (alphanumeric, <= 32 chars).

    The same strategy, instrument, side, bar, and purpose produce the same id,
    so a retry or a restart cannot double-send that intent.
    """
    raw = f"{strategy}|{inst_id}|{side}|{bar_ts}|{purpose}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return "c" + digest[:31]
