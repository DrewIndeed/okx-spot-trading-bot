"""Favorites universe + point-in-time monthly top-20 breadth helpers.

Copied in spirit from /workspace/crypto/research4.py `universe()` / `breadth`
(do not modify that file). Universe for trading is favorites.txt (49 USDT pairs),
NOT a live top-20-by-volume list. The top-20 is only a point-in-time membership
set used to compute breadth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from tradingbot.types import Candle

DEFAULT_FAVORITES = Path("data/universe_seed.txt")
B_SLOTS = 20
MIN_HISTORY_DAYS = 200
MEDIAN_QUOTE_VOL_MIN = 2_000_000.0  # $2M 30d median quote vol (cache field [6] / volCcy)
VOL_ROLL = 30
VOL_MIN_PERIODS = 20


def load_favorites(path: str | Path | None = None) -> list[str]:
    p = Path(path or DEFAULT_FAVORITES)
    text = p.read_text().strip()
    bases = [t for t in text.replace(",", " ").split() if t]
    # Normalize BTC to plain "BTC" (file may have "BTC-USDT" or "BTC")
    normed = []
    for b in bases:
        u = b.upper()
        if u == "BTC" or u == "BTC-USDT":
            normed.append("BTC")
        else:
            normed.append(b)
    bases = normed
    # BTC first, as in setup.py / paper_book.py / daily_bo.refresh
    if "BTC" in bases:
        bases = ["BTC"] + [s for s in bases if s != "BTC"]
    return bases


def inst_id(sym: str) -> str:
    s = sym.strip().upper()
    return s if s.endswith("-USDT") else f"{s}-USDT"


def base_sym(inst: str) -> str:
    return inst.upper().replace("-USDT", "")


def panel_from_candles(by_sym: dict[str, list[Candle]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build close / high / low / quote-volume / ts frames aligned on UTC dates (1Dutc) or timestamps.

    Quote volume uses candle.volume_quote, which is cache field [6] (volCcy) — same as research4 VQ.
    Only confirmed candles should be passed in.
    """
    def series(sym: str, attr: str) -> pd.Series:
        rows = by_sym[sym]
        idx = pd.to_datetime([c.ts for c in rows], unit="ms", utc=True).tz_localize(None)
        return pd.Series([getattr(c, attr) for c in rows], index=idx, name=base_sym(sym))

    cols = [base_sym(s) for s in by_sym]
    C = pd.concat([series(s, "close") for s in by_sym], axis=1)
    C.columns = cols
    HI = pd.concat([series(s, "high") for s in by_sym], axis=1)
    HI.columns = cols
    LO = pd.concat([series(s, "low") for s in by_sym], axis=1)
    LO.columns = cols
    VQ = pd.concat([series(s, "volume_quote") for s in by_sym], axis=1)
    VQ.columns = cols
    O = pd.concat([series(s, "open") for s in by_sym], axis=1)
    O.columns = cols
    return C.sort_index(), HI.sort_index(), LO.sort_index(), VQ.sort_index(), O.sort_index()


def sma(x: pd.Series | pd.DataFrame, n: int):
    return x.rolling(n, min_periods=n).mean()


def monthly_top20_universe(
    close: pd.DataFrame,
    quote_vol: pd.DataFrame,
    *,
    top_n: int = B_SLOTS,
    min_history: int = MIN_HISTORY_DAYS,
    median_vol_min: float = MEDIAN_QUOTE_VOL_MIN,
) -> pd.DataFrame:
    """Point-in-time monthly top-N by 30d average quote volume, recomputed on calendar day 1 using data at t-1.

    Eligibility: >= `min_history` days and 30d median quote vol >= `median_vol_min`.
    Logic copied from research4.universe(); not modified at the source.
    """
    C = close
    VQ = quote_vol.reindex(C.index)
    hist = C.notna().cumsum()
    mv = VQ.rolling(VOL_ROLL, min_periods=VOL_MIN_PERIODS).median()
    av = VQ.rolling(VOL_ROLL, min_periods=VOL_MIN_PERIODS).mean()
    U = pd.DataFrame(False, C.index, C.columns)
    cur = None
    for t in range(1, len(C)):
        d = C.index[t]
        if cur is None or d.day == 1:
            ok = (hist.iloc[t - 1] >= min_history) & (mv.iloc[t - 1] >= median_vol_min)
            picked = av.iloc[t - 1][ok].nlargest(top_n).index
            cur = list(picked)
        if cur:
            U.iloc[t, [C.columns.get_loc(c) for c in cur if c in C.columns]] = True
    return U


def breadth_top20(
    close: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    sma_n: int = 50,
) -> pd.Series:
    """Share of the point-in-time top-20 with close > SMA50. Copied from research4 line 32."""
    sma50 = sma(close, sma_n)
    denom = universe.sum(axis=1).replace(0, np.nan)
    return ((close > sma50) & universe).sum(axis=1) / denom


def top20_at(universe: pd.DataFrame, ts) -> list[str]:
    row = universe.loc[ts]
    return [c for c, v in row.items() if bool(v)]
