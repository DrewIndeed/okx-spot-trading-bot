"""Shared fixtures for the my_d1 / Telegram bot tests: synthetic closed candles, no network."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from tradingbot.config import load_config
from tradingbot.types import Candle

DAY_MS = 86_400_000
START_MS = 1_750_000_000_000 // DAY_MS * DAY_MS
SYMS = ("BTC", "ETH", "SOL")
REPO = Path(__file__).resolve().parents[1]


def series(base: str, n: int = 260, breakout: bool = True) -> list[Candle]:
    """Steady uptrend with a pullback, then a clean breakout on the last bar."""
    rng = np.random.default_rng(abs(hash(base)) % 2**32)
    px0 = {"BTC": 60_000.0, "ETH": 3_000.0, "SOL": 150.0}.get(base, 10.0)
    closes = px0 * np.cumprod(1 + 0.004 + rng.normal(0, 0.002, n))
    closes[-10:-1] = closes[-11] * np.linspace(0.995, 0.985, 9)  # pullback inside the channel
    if breakout:
        closes[-1] = closes[-11] * 1.05
    out = []
    for i, c in enumerate(closes):
        out.append(Candle(ts=START_MS + i * DAY_MS, open=float(c), high=float(c) * 1.01,
                          low=float(c) * 0.99, close=float(c), volume_base=1.0,
                          volume_quote=5_000_000.0, confirm=1))
    return out


class StubFeed:
    """Public-candle stub. It has no balance or order methods, so any such call would fail."""

    def __init__(self, extra_open_bar: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.extra_open_bar = extra_open_bar

    def get_candles(self, inst: str, bar: str, *, days=None, update=True, closed_only=True, **_):
        self.calls.append((inst, bar))
        base = inst.replace("-USDT", "")
        rows = series(base)
        if self.extra_open_bar and not closed_only:
            last = rows[-1]
            rows.append(Candle(ts=last.ts + DAY_MS, open=last.close, high=last.close * 3,
                               low=last.close, close=last.close * 3, volume_base=1.0,
                               volume_quote=5_000_000.0, confirm=0))
        return rows

    def get_spot_tickers(self):
        return {}


def make_cfg(tmp_path: Path, capital: float = 10_000.0):
    cfg = load_config(str(REPO / "config.yaml"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    uni = tmp_path / "universe.txt"
    uni.write_text("\n".join(SYMS))
    cfg.universe_file = str(uni)
    cfg.allocated_capital = float(capital)
    cfg.state_path = str(tmp_path / "state" / "portfolio.json")
    cfg.log_path = str(tmp_path / "logs" / "tradingbot.jsonl")
    cfg.paper_log_path = str(tmp_path / "logs" / "paper_book.jsonl")
    cfg.crypto_data_dir = str(tmp_path / "no_seed")
    cfg.data.cache_dir = str(tmp_path / "cache")
    return cfg
