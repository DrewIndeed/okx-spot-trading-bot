"""v3 seed universe and point-in-time top-N helpers.

The PIT top-N reuses tradingbot.universe.monthly_top20_universe (research4 logic).
Membership is seed-restricted; residual survivorship is documented, not zero.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from tradingbot.universe import (
    MEDIAN_QUOTE_VOL_MIN,
    MIN_HISTORY_DAYS,
    inst_id,
    load_favorites,
    monthly_top20_universe,
)

# Stables / pegs / wrapped — never in the v3 seed (data-contract §3).
STABLES = {
    "USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDG", "PYUSD", "USDE", "EUR", "EURC",
    "XAUT", "PAXG", "USD1", "RLUSD", "USDD", "USDP", "BUSD", "USDS", "FRAX", "LUSD",
    "GUSD", "EURT",
}
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "CBBTC", "BETH"}

# Historically liquid USDT-spot names on major venues since ~2021, tried against OKX.
# Missing instIds are recorded as fetch errors and dropped; this is not a claim of completeness.
HISTORICAL_SEED = (
    "BTC", "ETH", "LTC", "BCH", "BSV", "EOS", "XRP", "TRX", "ETC", "OKB", "LINK",
    "UNI", "SUSHI", "AAVE", "COMP", "YFI", "SNX", "CRV", "1INCH", "DOT", "KSM",
    "ADA", "XLM", "ZEC", "DASH", "NEO", "QTUM", "IOTA", "XTZ", "ATOM", "ALGO",
    "WAVES", "ONT", "IOST", "ZRX", "BAT", "SOL", "AVAX", "FTM", "NEAR", "FIL",
    "THETA", "DOGE", "SHIB", "MANA", "SAND", "AXS", "GALA", "CHZ", "ENJ", "FLOW",
    "ICP", "APE", "GMT", "LDO", "ARB", "OP", "SUI", "APT", "INJ", "PENDLE",
    "TON", "PEPE", "WLD", "ONDO", "ENA", "JUP", "STRK", "TIA", "SEI", "BLUR",
    "IMX", "RUNE", "STX", "MKR", "ENS", "LRC", "DYDX", "GMX", "BNB", "OKB",
    "CRO", "HBAR", "VET", "EGLD", "KAVA", "ZEC", "MINA", "CFG", "RAY", "AR",
    "GRT", "LPT", "RNDR", "RENDER", "FET", "ARKM", "WIF", "BONK", "FLOKI",
    "ORDI", "STETH",  # will be excluded by WRAPPED if present as STETH
    "MATIC", "POL", "MNT", "W", "JTO", "PYTH", "EIGEN", "ETHFI", "MORPHO",
    "TAO", "BOME", "NOT", "MEW", "POPCAT", "TRUMP", "S", "MOVE", "HYPE",
    "PUMP", "PENGU", "KITE", "VIRTUAL", "ZRO", "SKY", "LIT", "GRAM", "GRASS",
    "MON", "VVV", "ZAMA", "UNI",
)


def base_of(sym: str) -> str:
    return inst_id(sym).removesuffix("-USDT")


def is_excluded(sym: str) -> bool:
    inst = inst_id(sym)
    base = base_of(inst)
    if not inst.endswith("-USDT"):
        return True
    if base in STABLES or base in WRAPPED:
        return True
    # scan.py drops X*ON-USDT leveraged tokens
    if base.startswith("X") and base.endswith("ON") and len(base) > 3:
        return True
    return False


def seed_bases(
    *,
    favorites: Iterable[str] | None = None,
    historical: Iterable[str] | None = None,
    tickers: Iterable[str] | None = None,
    extra: Iterable[str] | None = None,
) -> list[str]:
    """Deduped base symbols, BTC first, stables/wrapped removed."""
    fav = list(favorites) if favorites is not None else load_favorites()
    hist = list(historical) if historical is not None else list(HISTORICAL_SEED)
    seen: list[str] = []
    for src in (fav, hist, list(tickers or []), list(extra or [])):
        for s in src:
            if is_excluded(s):
                continue
            b = base_of(s)
            if b not in seen:
                seen.append(b)
    if "BTC" in seen:
        seen = ["BTC"] + [s for s in seen if s != "BTC"]
    return seen


def seed_inst_ids(**kwargs) -> list[str]:
    return [inst_id(s) for s in seed_bases(**kwargs)]


def pit_topn(close, quote_vol, *, top_n: int = 20, min_history: int = MIN_HISTORY_DAYS,
             median_vol_min: float = MEDIAN_QUOTE_VOL_MIN):
    """Point-in-time monthly top-N. Recomputes on calendar day 1 using data at t-1."""
    return monthly_top20_universe(
        close, quote_vol, top_n=top_n, min_history=min_history, median_vol_min=median_vol_min
    )
