from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from okxspot.config import load_settings
from okxspot.errors import LiveGuardError
from okxspot.safety import (
    LIVE_CONFIRM_VALUE,
    extra_funds_warning,
    read_okx_keys,
    validate_live_preconditions,
    validate_permissions,
)
from tests.conftest import make_settings

D = Decimal


def _ok(**overrides: object) -> None:
    params: dict[str, object] = {
        "enable_live_flag": True,
        "live_enabled": True,
        "env_confirm": LIVE_CONFIRM_VALUE,
        "allocated_capital": D("1000"),
        "max_allocated_capital": D("1000"),
    }
    params.update(overrides)
    validate_live_preconditions(**params)  # type: ignore[arg-type]


def test_live_requires_flag_and_env() -> None:
    with pytest.raises(LiveGuardError, match="--enable-live"):
        _ok(enable_live_flag=False)
    with pytest.raises(LiveGuardError, match="OKX_LIVE_CONFIRM"):
        _ok(env_confirm=None)
    with pytest.raises(LiveGuardError, match="OKX_LIVE_CONFIRM"):
        _ok(env_confirm="yes")


def test_live_refuses_unset_or_oversized_allocated_capital() -> None:
    with pytest.raises(LiveGuardError, match="unset"):
        _ok(allocated_capital=None)
    with pytest.raises(LiveGuardError, match="positive"):
        _ok(allocated_capital=D("0"))
    with pytest.raises(LiveGuardError, match="exceeds"):
        _ok(allocated_capital=D("2000"), max_allocated_capital=D("1000"))
    with pytest.raises(LiveGuardError, match="max_allocated_capital"):
        _ok(max_allocated_capital=None)


def test_permissions_refuse_withdraw_and_unknown() -> None:
    with pytest.raises(LiveGuardError, match="withdraw"):
        validate_permissions({"read_only", "trade", "withdraw"})
    with pytest.raises(LiveGuardError, match="not detectable"):
        validate_permissions(set())
    with pytest.raises(LiveGuardError, match="not detectable"):
        validate_permissions(None)
    with pytest.raises(LiveGuardError, match="lacks trade"):
        validate_permissions({"read_only"})
    validate_permissions({"read_only", "trade"})


def test_keys_missing_and_repr_redacts() -> None:
    with pytest.raises(LiveGuardError, match="OKX_API_KEY"):
        read_okx_keys({})
    keys = read_okx_keys(
        {
            "OKX_API_KEY": "test-key",
            "OKX_API_SECRET": "test-secret",
            "OKX_API_PASSPHRASE": "test-pass",
        }
    )
    assert "test-secret" not in repr(keys)


def test_config_requires_allocated_capital_and_risk_band() -> None:
    with pytest.raises(ValidationError):
        make_settings(allocated_capital=None)
    with pytest.raises(ValidationError):
        make_settings(risk={"risk_per_trade": "0.02"})
    with pytest.raises(ValidationError):
        make_settings(risk={"risk_per_trade": "0.004"})
    with pytest.raises(ValidationError):
        make_settings(symbols=["BTC-USDT-SWAP"])
    loaded = load_settings("config/default.yaml")
    assert loaded.mode == "paper"
    assert loaded.allocated_capital == D("1000")
    assert loaded.fees.rate_for("backtest") == D("0.002")
    assert loaded.fees.rate_for("paper") == D("0.001")
    text = Path("config/default.yaml").read_text(encoding="utf-8")
    assert "OKX_API" not in text
    assert "BEGIN PRIVATE" not in text


def test_extra_funds_warning_does_not_change_size() -> None:
    assert extra_funds_warning(D("50000"), D("1000")) is not None
    assert extra_funds_warning(D("1000"), D("1000")) is None
    assert extra_funds_warning(None, D("1000")) is None
