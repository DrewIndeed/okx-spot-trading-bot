from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from okxspot.errors import LiveGuardError

# Both the flag and this exact env value are required. A yaml mode field is not enough.
LIVE_CONFIRM_ENV = "OKX_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_ACCEPT_LIVE_TRADING_LOSS_RISK"
LIVE_CONFIRM_ALIAS_ENV = "LIVE_CONFIRM"
LIVE_CONFIRM_ALIAS_VALUE = "I_ACCEPT"
ACCEPTED_LIVE_CONFIRMS = frozenset({LIVE_CONFIRM_VALUE, LIVE_CONFIRM_ALIAS_VALUE})
NETWORK_DISABLE_ENV = "OKXSPOT_DISABLE_NETWORK"
SUPERVISED_ENV = "OKXSPOT_SUPERVISED"


def confirm_live_env(env: Mapping[str, str]) -> str | None:
    """Return a recognized confirm value, or the raw value so the caller can reject it."""
    primary = (env.get(LIVE_CONFIRM_ENV) or "").strip()
    if primary == LIVE_CONFIRM_VALUE:
        return primary
    alias = (env.get(LIVE_CONFIRM_ALIAS_ENV) or "").strip()
    if alias == LIVE_CONFIRM_ALIAS_VALUE:
        return alias
    return primary or alias or None


def validate_live_preconditions(
    *,
    enable_live_flag: bool,
    env_confirm: str | None,
    allocated_capital: Decimal | None,
    max_allocated_capital: Decimal | None,
    live_enabled: bool = False,
) -> None:
    """Refuse live mode before any network call.

    ``allocated_capital`` is required and must be within the configured max.
    Missing, zero, or oversized caps never fall through to the account balance.
    ``live_enabled`` must be true in config; a YAML ``mode: live`` field is not enough.
    """
    if not enable_live_flag:
        raise LiveGuardError("live mode requires --enable-live")
    if not live_enabled:
        raise LiveGuardError("live mode requires live_enabled: true in config")
    if env_confirm not in ACCEPTED_LIVE_CONFIRMS:
        raise LiveGuardError(
            f"live mode requires environment variable {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE} "
            f"or {LIVE_CONFIRM_ALIAS_ENV}={LIVE_CONFIRM_ALIAS_VALUE}"
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


def validate_account_restrictions(account: Any, settings: Any) -> None:
    """Refuse live mode on the main account or on a key that is not IP-restricted."""
    require_ip = bool(getattr(settings, "require_ip_restriction", True))
    require_sub = bool(getattr(settings, "require_dedicated_subaccount", True))
    required_label = getattr(settings, "required_account_label", None) or None
    required_uid = getattr(settings, "required_uid", None) or None
    ip = getattr(account, "ip", None)
    ip_text = str(ip or "").strip()
    wild = {"*", "0.0.0.0", "0.0.0.0/0", "::/0"}
    if require_ip and (not ip_text or ip_text in wild or any(part.strip() in wild for part in ip_text.split(","))):
        raise LiveGuardError(
            "API key is not IP-restricted (or IP restriction was not detectable); refusing live mode"
        )
    uid = str(getattr(account, "uid", "") or "")
    main_uid = str(getattr(account, "main_uid", "") or "")
    if require_sub:
        if not uid or not main_uid:
            raise LiveGuardError("subaccount identity was not detectable; refusing live mode")
        if uid == main_uid:
            raise LiveGuardError(
                "API key is on the main account; live mode requires a dedicated subaccount"
            )
    label = str(getattr(account, "label", "") or "")
    if required_label and label != required_label:
        raise LiveGuardError("account label does not match required_account_label; refusing live mode")
    if required_uid and uid != required_uid:
        raise LiveGuardError("account uid does not match required_uid; refusing live mode")


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
