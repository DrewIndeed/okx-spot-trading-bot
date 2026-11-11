from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okxspot.backtest.download import download_candles
from okxspot.backtest.metrics import cagr, compute_metrics, max_drawdown, profit_factor
from okxspot.models import ClosedTrade

D = Decimal
START = datetime(2024, 1, 1, tzinfo=UTC)


def _trade(pnl: str, r: str) -> ClosedTrade:
    return ClosedTrade("BTC-USDT", D(pnl), D("10"), START, START + timedelta(days=2))


def test_drawdown_profit_factor_and_cagr() -> None:
    assert max_drawdown([D("100"), D("120"), D("90"), D("90")]) == D("30") / D("120")
    trades = [_trade("10", "1"), _trade("-5", "-0.5"), _trade("5", "0.5")]
    assert profit_factor(trades) == D("3")
    metrics = compute_metrics(
        [(START, D("100")), (START + timedelta(days=10), D("110"))],
        trades,
        exposed_bars=4,
        total_bars=8,
    )
    assert metrics.n_trades == 3
    assert metrics.win_rate == D("2") / D("3")
    assert metrics.exposure == D("0.5")
    assert metrics.expectancy_r == (D("1") + D("-0.5") + D("0.5")) / D("3")
    assert metrics.max_drawdown == D("0")
    assert cagr(D("100"), D("110"), D("365.25")) is not None
    assert profit_factor([]) is None


class _Source:
    def __init__(self) -> None:
        self.calls = 0
        self.rows = _rows(150)

    def fetch_history_page(
        self,
        inst_id: str,
        bar: str,
        after_ms: int | None,
        limit: int,
    ) -> list[list[str]]:
        self.calls += 1
        eligible = self.rows
        if after_ms is not None:
            eligible = [row for row in self.rows if int(row[0]) < after_ms]
        return eligible[:limit]


def _rows(n: int) -> list[list[str]]:
    out = []
    for index in range(n):
        ts = int((START + timedelta(days=index)).timestamp() * 1000)
        confirm = "0" if index == 3 else "1"
        out.append([str(ts), "100", "110", "90", "105", "1", "0", "0", confirm])
    out.reverse()
    return out


def test_downloader_pages_and_uses_cache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = _Source()
    end = START + timedelta(days=149)
    first = download_candles(
        source,
        "BTC-USDT",
        "1D",
        START,
        end,
        cache_dir=tmp_path,
        limit=100,
    )
    assert source.calls == 2
    assert all(candle.confirmed for candle in first)
    assert len(first) == 149
    second_source = _Source()
    second = download_candles(
        second_source,
        "BTC-USDT",
        "1D",
        START,
        end,
        cache_dir=tmp_path,
        limit=100,
    )
    assert second_source.calls == 0
    assert len(second) == len(first)
