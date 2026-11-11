from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from okxspot.errors import LiveGuardError

# Both the flag and this exact env value are required. A yaml mode field is not enough.
LIVE_CONFIRM_ENV = "OKX_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_ACCEPT_LIVE_TRADING_LOSS_RISK"
NETWORK_DISABLE_ENV = "OKXSPOT_DISABLE_NETWORK"


def validate_live_preconditions(
    *,
    enable_live_flag: bool,
    env_confirm: str | None,
    allocated_capital: Decimal | None,
    max_allocated_capital: Decimal | None,
) -> None:
    """Refuse live mode before any network call.

    ``allocated_capital`` is required and must be within the configured max.
    Missing, zero, or oversized caps never fall through to the account balance.
    """
    if not enable_live_flag:
        raise LiveGuardError("live mode requires --enable-live")
    if env_confirm != LIVE_CONFIRM_VALUE:
        raise LiveGuardError(
            f"live mode requires environment variable {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE}"
        )
    if allocated_capital is None:
        raise LiveGuardError("allocated_capital is unset; refusing live mode")
    if allocated_capital <= 0:
        raise LiveGuardError("allocated_capital must be positive; refusing live mode")
    if max_allocated_capital is None or max_allocated_capital <= 0:
        raise LiveGuardError("max_allocated_capital must be configured; refusing live mode")
    if allocated_capital > max_allocated_capital:
        raise LiveGuardError(
            "allocated_capital exceeds max_allocated_capital; refusing live mode"
        )


def validate_permissions(permissions: set[str] | None) -> None:
    """Fail closed unless the key is trade-capable and has no withdraw right.

    OKX reports API-key scope on ``GET /api/v5/account/config`` in ``perm``
    (comma-separated, e.g. ``read_only,trade``). Withdraw is detectable there.
    An empty or missing scope is treated as unknown and refused.
    """
    if permissions is None:
        raise LiveGuardError("API key permissions were not detectable; refusing live mode")
    perms = {item.strip().lower() for item in permissions if item and item.strip()}
    if not perms:
        raise LiveGuardError("API key permissions were not detectable; refusing live mode")
    if "withdraw" in perms:
        raise LiveGuardError("API key has withdraw permission; refusing live mode")
    if "trade" not in perms:
        raise LiveGuardError("API key lacks trade permission; refusing live mode")


class OkxKeys:
    def __init__(self, api_key: str, secret: str, passphrase: str) -> None:
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase

    def __repr__(self) -> str:
        return "OkxKeys(redacted)"


def read_okx_keys(env: Mapping[str, str]) -> OkxKeys:
    key = env.get("OKX_API_KEY", "").strip()
    secret = env.get("OKX_API_SECRET", "").strip()
    passphrase = env.get("OKX_API_PASSPHRASE", "").strip()
    if not key or not secret or not passphrase:
        raise LiveGuardError(
            "live mode requires OKX_API_KEY, OKX_API_SECRET, and OKX_API_PASSPHRASE"
        )
    return OkxKeys(key, secret, passphrase)


def extra_funds_warning(total_eq: Decimal | None, bot_equity: Decimal) -> str | None:
    """Warn when the venue shows equity above the bot's own book.

    The warning does not change size. Sizing ignores ``total_eq``.
    """
    if total_eq is None or bot_equity <= 0:
        return None
    if total_eq > bot_equity * Decimal("1.05"):
        return (
            "account totalEq exceeds allocated bot equity; sizing will not use the surplus. "
            "Run this engine in a dedicated OKX sub-account funded only with allocated capital."
        )
    return None
