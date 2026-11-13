from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from okxspot.decimals import parse_decimal
from okxspot.models import assert_spot_symbol


LIVE_MAX_DRAWDOWN_PCT = Decimal("0.15")


def _dec(value: object) -> Decimal:
    return parse_decimal(value)


class RiskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_per_trade: Decimal
    max_concurrent_positions: int = Field(ge=1)
    max_total_exposure_pct: Decimal
    per_coin_cap_pct: Decimal
    daily_loss_limit_pct: Decimal
    max_drawdown_pct: Decimal
    cooldown_bars_after_loss: int = Field(ge=0)

    @field_validator(
        "risk_per_trade",
        "max_total_exposure_pct",
        "per_coin_cap_pct",
        "daily_loss_limit_pct",
        "max_drawdown_pct",
        mode="before",
    )
    @classmethod
    def _parse_decimals(cls, value: object) -> Decimal:
        return _dec(value)

    @field_validator("risk_per_trade")
    @classmethod
    def _risk_band(cls, value: Decimal) -> Decimal:
        if value < Decimal("0.005") or value > Decimal("0.01"):
            raise ValueError("risk_per_trade must be between 0.005 and 0.01 (0.5% to 1%)")
        return value

    @field_validator(
        "max_total_exposure_pct",
        "per_coin_cap_pct",
        "daily_loss_limit_pct",
        "max_drawdown_pct",
    )
    @classmethod
    def _pct(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > 1:
            raise ValueError("percentage limits must be in (0, 1]")
        return value


class FeeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taker_fee: Decimal
    backtest_fee_multiplier: Decimal = Decimal("2")
    paper_fee_multiplier: Decimal = Decimal("1")
    slippage_bps: Decimal = Decimal("0")
    partial_fill_ratio: Decimal = Decimal("1")

    @field_validator(
        "taker_fee",
        "backtest_fee_multiplier",
        "paper_fee_multiplier",
        "slippage_bps",
        "partial_fill_ratio",
        mode="before",
    )
    @classmethod
    def _parse_decimals(cls, value: object) -> Decimal:
        return _dec(value)

    @model_validator(mode="after")
    def _bounds(self) -> FeeSettings:
        if self.taker_fee < 0 or self.taker_fee > Decimal("0.02"):
            raise ValueError("taker_fee must be between 0 and 0.02")
        if self.backtest_fee_multiplier < 1 or self.paper_fee_multiplier < 0:
            raise ValueError("fee multipliers must be non-negative; backtest multiplier >= 1")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if self.partial_fill_ratio <= 0 or self.partial_fill_ratio > 1:
            raise ValueError("partial_fill_ratio must be in (0, 1]")
        return self

    def rate_for(self, mode: Literal["backtest", "paper", "live"]) -> Decimal:
        if mode == "backtest":
            return self.taker_fee * self.backtest_fee_multiplier
        if mode == "paper":
            return self.taker_fee * self.paper_fee_multiplier
        return self.taker_fee


class StrategySettings(BaseModel):
    """Parameters for the placeholder Donchian strategy.

    The name is fixed so a typo cannot silently load a different model.
    The rules are a placeholder pending a researched specification.
    """

    model_config = ConfigDict(extra="forbid")

    name: Literal["donchian_atr"] = "donchian_atr"
    donchian_n: int = Field(ge=2)
    atr_n: int = Field(ge=2)
    atr_mult: Decimal
    regime_sma: int = Field(ge=2)

    @field_validator("atr_mult", mode="before")
    @classmethod
    def _parse_atr(cls, value: object) -> Decimal:
        return _dec(value)

    @field_validator("atr_mult")
    @classmethod
    def _atr_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("atr_mult must be positive")
        return value


class OkxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rest_base: str = "https://www.okx.com"
    ws_public: str = "wss://ws.okx.com:8443/ws/v5/public"
    timeout_s: float = Field(default=10, gt=0)
    max_retries: int = Field(default=5, ge=1)
    rate_limit_per_sec: float = Field(default=5, gt=0)
    min_notional_usdt: Decimal = Decimal("1")

    @field_validator("min_notional_usdt", mode="before")
    @classmethod
    def _parse_min(cls, value: object) -> Decimal:
        return _dec(value)


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class StateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sqlite_path: str = "data/state.db"


class BacktestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oos_ratio: Decimal = Decimal("0.30")
    cache_dir: str = "data/cache"

    @field_validator("oos_ratio", mode="before")
    @classmethod
    def _parse_oos(cls, value: object) -> Decimal:
        return _dec(value)

    @field_validator("oos_ratio")
    @classmethod
    def _oos_band(cls, value: Decimal) -> Decimal:
        if value <= 0 or value >= 1:
            raise ValueError("oos_ratio must be between 0 and 1")
        return value


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_seconds: float = Field(default=30, gt=0)


class Settings(BaseModel):
    """Engine configuration.

    ``allocated_capital`` is a fixed USDT cap. It is required. Sizing never
    uses total account equity. Live mode additionally refuses to start when
    this cap is above ``max_allocated_capital``.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["paper", "live", "backtest"] = "paper"
    allocated_capital: Decimal
    max_allocated_capital: Decimal
    quote_ccy: str = "USDT"
    symbols: list[str]
    bar: str = "1D"
    regime_symbol: str = "BTC-USDT"
    risk: RiskSettings
    fees: FeeSettings
    strategy: StrategySettings
    okx: OkxSettings = OkxSettings()
    telegram: TelegramSettings = TelegramSettings()
    state: StateSettings = StateSettings()
    backtest: BacktestSettings = BacktestSettings()
    runtime: RuntimeSettings = RuntimeSettings()

    @field_validator("allocated_capital", "max_allocated_capital", mode="before")
    @classmethod
    def _parse_caps(cls, value: object) -> Decimal:
        return _dec(value)

    @field_validator("allocated_capital", "max_allocated_capital")
    @classmethod
    def _positive_caps(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("capital caps must be positive")
        return value

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one symbol is required")
        for symbol in value:
            assert_spot_symbol(symbol)
        return value

    @field_validator("regime_symbol")
    @classmethod
    def _regime(cls, value: str) -> str:
        assert_spot_symbol(value)
        return value

    @model_validator(mode="after")
    def _live_drawdown_cap(self) -> Settings:
        # A paper kill switch above 15% must never carry over to a live config.
        if self.mode == "live" and self.risk.max_drawdown_pct > LIVE_MAX_DRAWDOWN_PCT:
            raise ValueError(
                f"live mode needs its own max_drawdown_pct <= {LIVE_MAX_DRAWDOWN_PCT}; "
                f"got {self.risk.max_drawdown_pct}"
            )
        return self

    @field_validator("quote_ccy")
    @classmethod
    def _quote(cls, value: str) -> str:
        if value != "USDT":
            raise ValueError("this build quotes PnL and allocated capital in USDT")
        return value


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config file must be a YAML mapping")
    return Settings.model_validate(raw)
