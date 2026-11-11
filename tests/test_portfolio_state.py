from tradingbot.orders import OrderManager
from tradingbot.portfolio import Portfolio
from tradingbot.types import OrderIntent


def test_state_survives_restart(tmp_path):
    path = tmp_path / "portfolio.json"
    book = Portfolio.new(10_000, path=str(path), day="2026-11-11")
    om = OrderManager(fee_taker=0.001, slippage=0.002)
    om.submit_paper(
        book,
        OrderIntent(
            inst_id="BTC-USDT",
            side="buy",
            qty=0.5,
            price=100.0,
            ts=1,
            signal_id="sig-restart",
            client_oid="tb-restart",
            stop=90.0,
            initial_risk=5.0,
        ),
    )
    book.halted = False
    book.save()

    loaded = Portfolio.load(str(path), allocated_capital=10_000)
    assert loaded.allocated_capital == 10_000
    assert loaded.cash == book.cash
    assert loaded.positions["BTC-USDT"].qty == 0.5
    assert loaded.positions["BTC-USDT"].avg_px == book.positions["BTC-USDT"].avg_px
    assert loaded.positions["BTC-USDT"].stop == 90.0
    assert "sig-restart" in loaded.used_signal_ids
    assert "tb-restart" in loaded.used_client_oids
    assert loaded.day == "2026-11-11"


def test_load_missing_file_starts_at_allocated(tmp_path):
    path = tmp_path / "missing.json"
    book = Portfolio.load(str(path), allocated_capital=2500)
    assert book.cash == 2500
    assert book.allocated_capital == 2500
    assert book.positions == {}
