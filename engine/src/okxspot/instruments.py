from __future__ import annotations

from decimal import Decimal

from okxspot.models import Instrument, assert_spot_symbol


def spot_instrument(inst_id: str, min_notional: Decimal) -> Instrument:
    """Coarse default lot sizes for offline paper and tests.

    Live mode loads tick size, lot size, and min size from OKX instrument info.
    """
    assert_spot_symbol(inst_id)
    base, quote = inst_id.split("-")
    if base == "BTC":
        lot = Decimal("0.00001")
        tick = Decimal("0.1")
    else:
        lot = Decimal("0.0001")
        tick = Decimal("0.01")
    return Instrument(
        inst_id=inst_id,
        base_ccy=base,
        quote_ccy=quote,
        tick_sz=tick,
        lot_sz=lot,
        min_sz=lot,
        min_notional=min_notional,
    )


def default_instruments(symbols: list[str], min_notional: Decimal) -> dict[str, Instrument]:
    return {symbol: spot_instrument(symbol, min_notional) for symbol in symbols}
