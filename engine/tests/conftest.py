from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from okxspot.config import Settings
from okxspot.models import Candle, Instrument
from okxspot.safety import NETWORK_DISABLE_ENV


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NETWORK_DISABLE_ENV, "1")


def _merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def make_settings(**overrides: object) -> Settings:
    raw: dict[str, object] = {
        "allocated_capital": "1000",
        "max_allocated_capital": "1000",
        "symbols": ["BTC-USDT"],
        "regime_symbol": "BTC-USDT",
        "risk": {
            "risk_per_trade": "0.01",
            "max_concurrent_positions": 3,
            "max_total_exposure_pct": "1",
            "per_coin_cap_pct": "1",
            "daily_loss_limit_pct": "0.03",
            "max_drawdown_pct": "0.15",
            "cooldown_bars_after_loss": 3,
        },
        "fees": {
            "taker_fee": "0.001",
            "backtest_fee_multiplier": "2",
            "paper_fee_multiplier": "1",
            "slippage_bps": "0",
            "partial_fill_ratio": "1",
        },
        "strategy": {
            "name": "donchian_atr",
            "donchian_n": 20,
            "atr_n": 14,
            "atr_mult": "2",
            "regime_sma": 200,
        },
    }
    return Settings.model_validate(_merge(raw, overrides))


def instrument(**overrides: object) -> Instrument:
    data: dict[str, object] = {
        "inst_id": "BTC-USDT",
        "base_ccy": "BTC",
        "quote_ccy": "USDT",
        "tick_sz": Decimal("0.01"),
        "lot_sz": Decimal("0.001"),
        "min_sz": Decimal("0.001"),
        "min_notional": Decimal("5"),
    }
    data.update(overrides)
    return Instrument(
        inst_id=str(data["inst_id"]),
        base_ccy=str(data["base_ccy"]),
        quote_ccy=str(data["quote_ccy"]),
        tick_sz=Decimal(str(data["tick_sz"])),
        lot_sz=Decimal(str(data["lot_sz"])),
        min_sz=Decimal(str(data["min_sz"])),
        min_notional=Decimal(str(data["min_notional"])),
    )


def day(offset: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=offset)


def candle(
    offset: int,
    price: str = "100",
    *,
    inst: str = "BTC-USDT",
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    close: str | None = None,
) -> Candle:
    open_px = Decimal(open_ if open_ is not None else price)
    close_px = Decimal(close if close is not None else price)
    high_px = Decimal(high) if high is not None else max(open_px, close_px)
    low_px = Decimal(low) if low is not None else min(open_px, close_px)
    return Candle(inst, day(offset), open_px, high_px, low_px, close_px, Decimal("1"), True)


def write_config(directory: Path, **overrides: object) -> Path:
    settings = make_settings(**overrides)
    path = directory / "config.yaml"
    # Round-trip through the defaults file is unnecessary; dump the model as YAML-ish JSON
    # is not valid for the loader. Write a small YAML document instead.
    import yaml

    path.write_text(yaml.safe_dump(settings.model_dump(mode="json")), encoding="utf-8")
    return path
