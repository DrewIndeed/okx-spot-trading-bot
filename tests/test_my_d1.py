"""my_d1: closed bars only, sizing from allocated_capital, wedding refusal, no live path, kill switch."""
from __future__ import annotations

import inspect

import pytest

from tests._botkit import DAY_MS, StubFeed, make_cfg, series
from tradingbot import cli
from tradingbot.data.rest import OkxRestFeed
from tradingbot.data.store import save_candles
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskManager
from tradingbot.strategy import my_d1
from tradingbot.strategy.my_d1 import InvalidParameterError, MyD1Strategy
from tradingbot.types import Candle, Signal
from tradingbot.universe import panel_from_candles


@pytest.mark.parametrize("cap", [None, 0, -1, 6788, 6788.0])
def test_refuses_bad_or_wedding_capital(cap):
    with pytest.raises(InvalidParameterError):
        MyD1Strategy(allocated_capital=cap)


def test_closed_bars_only_from_cache(tmp_path):
    cfg = make_cfg(tmp_path)
    for base in ("BTC", "ETH", "SOL"):
        rows = series(base)
        last = rows[-1]
        rows.append(Candle(ts=last.ts + DAY_MS, open=last.close, high=last.close * 5, low=last.close,
                           close=last.close * 5, volume_base=1, volume_quote=5e6, confirm=0))
        save_candles(cfg.data.cache_dir, f"{base}-USDT", "1Dutc", rows)
    feed = OkxRestFeed(cache_dir=cfg.data.cache_dir, crypto_data_dir=cfg.crypto_data_dir)
    by, missing = cli._load_d1_panel(feed, cfg, days=400, update=False)
    assert missing == []
    C, HI, LO, VQ, _O = panel_from_candles(by)
    assert int(C.index[-1].value // 1_000_000) == series("BTC")[-1].ts  # open bar dropped
    strat = MyD1Strategy(allocated_capital=cfg.allocated_capital, breadth_min=0.5)
    strat.prepare(C, high=HI, low=LO, quote_vol=VQ)
    w, _ = strat.weights_and_trades(state={})
    assert len(w) == len(C) and (w.iloc[-1] >= 0).all()


def _run(tmp_path, monkeypatch, capital):
    cfg = make_cfg(tmp_path / str(int(capital)), capital=capital)
    monkeypatch.setattr(cli, "_feed", lambda _cfg: StubFeed())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    rc, summary = cli.run_paper_iteration(cfg, strategy="my_d1", update=False)
    assert rc == 0, summary
    book = Portfolio.load(cfg.state_path)
    notional = sum(p.qty * p.avg_px for p in book.positions.values())
    return summary, book, notional


def test_sizing_scales_with_allocated_capital_only(tmp_path, monkeypatch):
    s1, b1, n1 = _run(tmp_path, monkeypatch, 10_000.0)
    s2, b2, n2 = _run(tmp_path, monkeypatch, 20_000.0)
    assert s1["allocated_capital"] == 10_000.0 and b1.allocated_capital == 10_000.0
    assert n1 > 0, "synthetic breakout should open at least one paper position"
    assert n2 == pytest.approx(2 * n1, rel=0.05)
    assert n1 <= 10_000.0


def test_no_live_or_private_api_path():
    src = inspect.getsource(my_d1)
    for banned in ("place_order", "okx_exchange", "api_key", "passphrase", "withdraw", "/account/", "/trade/"):
        assert banned not in src
    assert cli.main(["--live", "paper-once", "--strategy", "my_d1", "--no-update"]) == 3


def _sig(i: int) -> Signal:
    return Signal(inst_id="BTC-USDT", side="buy", ts=i, signal_id=f"t:{i}", stop_price=90.0, strategy="MY_D1")


def test_kill_switch_trips_at_max_drawdown_not_before(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.risk.max_drawdown == pytest.approx(0.30)  # paper-only value, Cypheous-approved
    rm = RiskManager(cfg.risk, cfg.allocated_capital)
    book = Portfolio.new(10_000.0)
    book.equity_peak = 10_000.0
    book.day_start_equity = 7_200.0
    book.cash = 7_200.0  # -28%: watch-level breach, still trading
    assert rm.evaluate(_sig(1), book, 100.0, marks={}).reason != "kill_switch_max_drawdown"
    assert not book.halted
    book.cash = 6_990.0  # -30.1%
    book.day_start_equity = 6_990.0
    dec = rm.evaluate(_sig(2), book, 100.0, marks={})
    assert not dec.allowed and dec.reason == "kill_switch_max_drawdown" and book.halted


def test_user_facing_window_label_follows_lookback(tmp_path, monkeypatch, capsys):
    from tests._botkit import REPO
    from tradingbot import side_signals, summary_view
    from tradingbot.data.rest import OkxRestFeed
    from tradingbot.strategy.my_d1 import DEFAULT_LOOKBACK

    assert DEFAULT_LOOKBACK == 8  # Andrew's tuned value
    assert cli.build_parser().parse_args(["paper-once", "--strategy", "my_d1"]).lookback == DEFAULT_LOOKBACK
    assert cli.build_parser().parse_args(["summary"]).lookback == DEFAULT_LOOKBACK
    assert cli.build_parser().parse_args(["signals"]).lookback == DEFAULT_LOOKBACK

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(cli, "_feed", lambda _cfg: StubFeed())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for n in (8, 12):
        _rc, s = cli.run_paper_iteration(cfg, strategy="my_d1", update=False, lookback=n)
        if "breakout open" in s["message"]:
            assert f"{n}-day" in s["message"] and "20-day" not in s["message"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(side_signals, "_feed", lambda _cfg: StubFeed())
    for n in (8, 12):
        assert side_signals.print_signals(cfg, update=False, days=400, lookback=n, symbol="BTC") in (0, 1)
        out = capsys.readouterr().out
        assert "20-day" not in out

    for b in ("BTC", "ETH", "SOL"):
        save_candles(cfg.data.cache_dir, f"{b}-USDT", "1Dutc", series(b), backend="parquet")
    monkeypatch.setattr(OkxRestFeed, "get_spot_tickers", lambda self: {})
    for n in (8, 12):
        assert summary_view.print_summary(cfg, top=3, lookback=n) == 0
        out = capsys.readouterr().out
        assert f"under the {n}-day high" in out and "20-day" not in out
