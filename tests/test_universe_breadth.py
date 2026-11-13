import numpy as np
import pytest
import pandas as pd

from tradingbot.regime import d1_entry_regime, regime_at
from tradingbot.universe import breadth_top20, load_favorites, monthly_top20_universe


def test_favorites_includes_hype_ondo_and_btc_first():
    favs = load_favorites()
    assert favs[0] == "BTC"
    assert "HYPE" in favs and "ONDO" in favs
    assert len(favs) >= 49  # original core; seed grew to 125 USDT pairs
    assert "USDT" not in favs  # bases only, no stables


def test_point_in_time_monthly_top20_and_breadth():
    # 25 names, 250 days starting 2024-01-01. On the 1st of each month, rank by 30d avg quote vol.
    idx = pd.date_range("2024-01-01", periods=250, freq="D")
    cols = [f"C{i:02d}" for i in range(25)]
    # closes: C00..C09 always above a rising SMA? we'll set later
    close = pd.DataFrame(100.0, index=idx, columns=cols)
    # quote vol: first 20 names have 3e6, last 5 have 1e5 (below $2M median -> ineligible)
    vq = pd.DataFrame(3e6, index=idx, columns=cols)
    for c in cols[20:]:
        vq[c] = 1e5
    # make C00 the highest volume
    vq["C00"] = 9e6
    U = monthly_top20_universe(close, vq, min_history=30, median_vol_min=2e6)
    # After enough history, day-1 recomputes; mid-sample should have 20 members, not the illiquid 5
    mid = idx[80]
    members = [c for c in cols if bool(U.loc[mid, c])]
    assert len(members) == 20
    assert "C00" in members
    assert "C24" not in members

    # breadth: last 60 days, first 10 names jump to 200 (above SMA50), rest drop to 50
    # only the last 10 days move, so SMA50 still lags and close > SMA50 can be True
    close.loc[idx[-10]:, cols[:10]] = 200.0
    close.loc[idx[-10]:, cols[10:]] = 50.0
    br = breadth_top20(close, U)
    # top-20 are C00-C19; C00-C09 above SMA50, C10-C19 below -> breadth 0.5
    assert br.iloc[-1] == pytest.approx(0.5)



def test_regime_helper_and():
    idx = pd.date_range("2023-01-01", periods=250, freq="D")
    btc = pd.Series(np.linspace(10, 30, 250), index=idx)  # rising, above SMA200 at the end
    breadth = pd.Series(0.6, index=idx)
    breadth.iloc[-5:] = 0.4
    combo = d1_entry_regime(btc, breadth)
    # late bars: BTC above SMA200 but breadth < 0.5 -> no new entries
    st = regime_at(btc, breadth, idx[-1])
    assert st.btc_above_sma200 is True
    assert st.breadth_ok is False
    assert st.allow_new_entries is False
    assert st.allow_hold is True
    # a bar with breadth 0.6 should allow entries
    st2 = regime_at(btc, breadth, idx[-6])
    assert st2.allow_new_entries is True
