import pytest

from tradingbot.config import Config
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskManager, make_client_oid
from tradingbot.types import Position, Signal


def _rm(capital=10_000, **risk_over):
    cfg = Config(allocated_capital=capital)
    for k, v in risk_over.items():
        setattr(cfg.risk, k, v)
    return cfg, RiskManager(cfg.risk, capital), Portfolio.new(capital)


def _buy(sid="sig-a", stop=90.0, inst="BTC-USDT"):
    return Signal(inst_id=inst, side="buy", ts=1, signal_id=sid, stop_price=stop)


def test_fixed_fraction_sizing_and_lot_rounding():
    cfg, rm, book = _rm(10_000, risk_pct=0.01, lot_size=0.1)
    rm.risk_pct = 0.01
    d = rm.evaluate(_buy("s1"), book, price=100.0)
    assert d.allowed
    # 10 coins, already on lot 0.1
    assert d.qty == pytest.approx(10.0)
    # odd lot floors
    cfg, rm, book = _rm(10_000, risk_pct=0.01, lot_size=3.0)
    rm.risk_pct = 0.01
    d = rm.evaluate(_buy("s2"), book, price=100.0)
    assert d.qty == pytest.approx(9.0)  # floor(10/3)*3


def test_min_notional_reject():
    cfg, rm, book = _rm(10_000, risk_pct=0.01, min_notional=10_000)
    rm.risk_pct = 0.01
    d = rm.evaluate(_buy(), book, price=100.0)  # notional 1000
    assert not d.allowed
    assert d.reason == "min_notional"


def test_exposure_caps():
    cfg, rm, book = _rm(10_000, max_exposure_asset=0.05, risk_pct=0.01)
    rm.risk_pct = 0.01
    d = rm.evaluate(_buy(), book, price=100.0)
    assert d.allowed
    # uncapped would be 10 qty = 1000 ntl; cap 5% of 10k = 500 -> 5 qty
    assert d.qty == pytest.approx(5.0)


def test_max_open_positions():
    cfg, rm, book = _rm(10_000, max_open_positions=1)
    book.positions["ETH-USDT"] = Position(inst_id="ETH-USDT", qty=1, avg_px=1)
    d = rm.evaluate(_buy(inst="BTC-USDT"), book, price=100.0)
    assert not d.allowed
    assert d.reason == "max_open_positions"


def test_kill_switch_max_drawdown():
    cfg, rm, book = _rm(10_000, max_drawdown=0.10)
    book.equity_peak = 10_000
    book.cash = 8_999  # 11.01% DD
    d = rm.evaluate(_buy(), book, price=100.0)
    assert not d.allowed
    assert "max_drawdown" in d.reason
    assert book.halted


def test_daily_loss_cap():
    cfg, rm, book = _rm(10_000, daily_loss_cap=0.03)
    book.day_start_equity = 10_000
    book.cash = 9_600  # 4% down
    d = rm.evaluate(_buy("dl"), book, price=100.0)
    assert not d.allowed
    assert d.reason == "daily_loss_cap"
    assert book.halted


def test_duplicate_signal_and_oid_guard():
    cfg, rm, book = _rm(10_000)
    rm.risk_pct = 0.01
    sig = _buy("dup-1")
    d1 = rm.evaluate(sig, book, price=100.0)
    assert d1.allowed
    book.remember_ids(sig.signal_id, d1.client_oid)
    d2 = rm.evaluate(sig, book, price=100.0)
    assert not d2.allowed
    assert d2.reason == "duplicate_signal"
    # same oid, different signal id still rejected
    other = _buy("dup-2")
    # force same oid by using same signal? different id -> different oid.
    # inject oid into used list
    book.used_client_oids.append(make_client_oid("dup-2"))
    d3 = rm.evaluate(other, book, price=100.0)
    assert not d3.allowed
    assert d3.reason == "duplicate_client_oid"
