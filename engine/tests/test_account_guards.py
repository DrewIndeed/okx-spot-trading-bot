from __future__ import annotations

from decimal import Decimal

import pytest

from okxspot.errors import LiveGuardError
from okxspot.exchange.okx_parse import parse_account_config
from okxspot.safety import (
    LIVE_CONFIRM_VALUE,
    confirm_live_env,
    validate_account_restrictions,
    validate_live_preconditions,
)
from tests.conftest import make_settings

D = Decimal


def _pre(**overrides: object) -> None:
    params: dict[str, object] = {
        "enable_live_flag": True,
        "live_enabled": True,
        "env_confirm": LIVE_CONFIRM_VALUE,
        "allocated_capital": D("1000"),
        "max_allocated_capital": D("1000"),
    }
    params.update(overrides)
    validate_live_preconditions(**params)  # type: ignore[arg-type]


def test_live_requires_live_enabled_config() -> None:
    with pytest.raises(LiveGuardError, match="live_enabled"):
        _pre(live_enabled=False)
    _pre(live_enabled=True)


def test_live_confirm_alias_i_accept() -> None:
    _pre(env_confirm=confirm_live_env({"LIVE_CONFIRM": "I_ACCEPT"}))
    with pytest.raises(LiveGuardError, match="LIVE_CONFIRM|OKX_LIVE_CONFIRM"):
        _pre(env_confirm=confirm_live_env({"LIVE_CONFIRM": "yes"}))


def test_parse_account_config_and_ip_restriction() -> None:
    cfg = parse_account_config(
        {
            "code": "0",
            "data": [
                {
                    "perm": "read_only,trade",
                    "ip": "1.2.3.4",
                    "uid": "sub-1",
                    "mainUid": "main-9",
                    "label": "paper-bot",
                }
            ],
        }
    )
    assert cfg.permissions == {"read_only", "trade"}
    assert cfg.ip == "1.2.3.4"
    assert cfg.uid == "sub-1"
    assert cfg.main_uid == "main-9"
    assert cfg.label == "paper-bot"
    settings = make_settings(
        live_enabled=True,
        require_dedicated_subaccount=True,
        require_ip_restriction=True,
        required_account_label="paper-bot",
        required_uid="sub-1",
    )
    validate_account_restrictions(cfg, settings)


def test_refuses_unrestricted_or_undetectable_ip() -> None:
    settings = make_settings(live_enabled=True, require_ip_restriction=True, require_dedicated_subaccount=False)
    empty = parse_account_config({"data": [{"perm": "trade", "ip": "", "uid": "u", "mainUid": "m"}]})
    with pytest.raises(LiveGuardError, match="IP"):
        validate_account_restrictions(empty, settings)
    missing = parse_account_config({"data": [{"perm": "trade", "uid": "u", "mainUid": "m"}]})
    with pytest.raises(LiveGuardError, match="IP"):
        validate_account_restrictions(missing, settings)


def test_refuses_main_account_and_label_mismatch() -> None:
    settings = make_settings(
        live_enabled=True,
        require_dedicated_subaccount=True,
        require_ip_restriction=True,
        required_account_label="bot-sub",
    )
    main = parse_account_config(
        {"data": [{"perm": "trade", "ip": "1.1.1.1", "uid": "same", "mainUid": "same", "label": "bot-sub"}]}
    )
    with pytest.raises(LiveGuardError, match="subaccount|main"):
        validate_account_restrictions(main, settings)
    wrong = parse_account_config(
        {"data": [{"perm": "trade", "ip": "1.1.1.1", "uid": "sub", "mainUid": "main", "label": "other"}]}
    )
    with pytest.raises(LiveGuardError, match="label"):
        validate_account_restrictions(wrong, settings)
