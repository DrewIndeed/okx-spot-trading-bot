import pytest

from tradingbot.config import LiveTradingRefused
from tradingbot.orders import OrderManager
from tradingbot.portfolio import Portfolio
from tradingbot.types import OrderIntent


def test_buy_fee_and_slippage():
    om = OrderManager(fee_taker=0.001, slippage=0.002)  # 2x fee
    book = Portfolio.new(10_000)
    intent = OrderIntent(
        inst_id="BTC-USDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts=1,
        signal_id="s1",
        client_oid="tb1",
        stop=90.0,
        initial_risk=10.0,
    )
    fill = om.submit_paper(book, intent)
    assert fill.price == pytest.approx(100.2)
    assert fill.notional == pytest.approx(100.2)
    assert fill.fee == pytest.approx(100.2 * 0.001)
    assert fill.slippage == pytest.approx(0.2)
    # cash reduced by notional + fee
    assert book.cash == pytest.approx(10_000 - 100.2 - fill.fee)
    assert book.positions["BTC-USDT"].qty == pytest.approx(1.0)
    assert book.positions["BTC-USDT"].avg_px == pytest.approx(100.2)


def test_sell_fee_and_slippage_and_pnl():
    om = OrderManager(fee_taker=0.001, slippage=0.002)
    book = Portfolio.new(10_000)
    om.submit_paper(
        book,
        OrderIntent(inst_id="BTC-USDT", side="buy", qty=1.0, price=100.0, ts=1,
                    signal_id="in", client_oid="in", stop=90.0, initial_risk=10.0),
    )
    # entry fill 100.2, fee 0.1002
    fill = om.submit_paper(
        book,
        OrderIntent(inst_id="BTC-USDT", side="sell", qty=1.0, price=110.0, ts=2,
                    signal_id="out", client_oid="out", is_exit=True, initial_risk=10.0),
    )
    assert fill.price == pytest.approx(110.0 * 0.998)
    assert fill.fee == pytest.approx(fill.price * 0.001)
    assert fill.is_exit
    # pnl = 1*(sell_px - avg_px) - sell_fee
    assert fill.pnl == pytest.approx(fill.price - 100.2 - fill.fee)
    assert "BTC-USDT" not in book.positions


def test_no_live_path():
    with pytest.raises(LiveTradingRefused):
        OrderManager().submit_live()
