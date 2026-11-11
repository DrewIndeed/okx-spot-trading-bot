"""Official v3 hypotheses H1–H5 (strategy-v3-brief). Do not add variants or retune."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from tradingbot.eval.engine import _apply_regime_to_new_risk, vc_targets
from tradingbot.eval.v3_engine import (
    apply_increase_block,
    clip_caps,
    common_pre,
    scale_to_vol,
    vol_target_weights,
)
from tradingbot.strategies import spec_v1_common as P1
from tradingbot.strategies import spec_v2_common as P2
from tradingbot.strategies.spec_v1_signals import universe_and_breadth, vc_signals, vol_ann
from tradingbot.strategies.spec_v2_core import core_signals
from tradingbot.universe import sma

HYPOTHESIS_IDS = ("H1", "H2", "H3", "H4", "H5")
NAMES = {
    "H1": "FUND-CROWD",
    "H2": "RS-ROTATE",
    "H3": "CORE-SLOW",
    "H4": "VOL-GATE",
    "H5": "F2-SOFT",
}


@dataclass(frozen=True)
class Hypothesis:
    id: str
    name: str
    symbols: tuple[str, ...] | None
    use_band: bool
    band_membership_only: bool
    describe: str
    precompute: Callable
    target_fn: Callable
    params: dict


def precompute_vc(panel: dict) -> dict:
    U, breadth = universe_and_breadth(panel)
    return {
        "U": U,
        "breadth": breadth,
        "vol": vol_ann(panel["close"]),
        "log_rets": np.log(panel["close"]).diff(),
        "vc": vc_signals(panel, U),
    }


def vc_state_strict(row: pd.Series) -> str:
    """Frozen V-C: F2 HALT or BTC stale → HALT; no F1."""
    if bool(row.get("f2_halt") or row.get("btc_stale_halt")):
        return "HALT"
    return "NORMAL"


def vc_state_soft(row: pd.Series) -> str:
    """H5: F2 is REDUCE 0.5; stale remains HALT; no force-exit (handled by increase-only clamp)."""
    if bool(row.get("btc_stale_halt")):
        return "HALT"
    if bool(row.get("f2_halt")):
        return "REDUCE"
    return "NORMAL"


def _vc_desired(t, panel, pre, current_w, state: str) -> pd.Series:
    desired = vc_targets(t, panel, pre, current_w, allow_new=state != "HALT")
    return _apply_regime_to_new_risk(desired, current_w, state)


def make_h1_target(crowd: pd.DataFrame):
    def fn(t, panel, pre, current_w, regime_state):
        # regime_state is unused; V-C uses F2 from the regime row stored in pre["regime"]
        row = pre["regime"].loc[t]
        state = vc_state_strict(row)
        desired = _vc_desired(t, panel, pre, current_w, state)
        if t not in crowd.index:
            return desired
        c = crowd.loc[t]
        blocked: set[str] = set()
        # Stale >3d → skip this filter (do not invent a rate; do not HALT the book).
        if not bool(c["btc_skip"]) and bool(c["btc_crowd"]):
            blocked.update(s for s in desired.index if s != "ETH")
        if not bool(c["eth_skip"]) and bool(c["eth_crowd"]):
            blocked.add("ETH")
        return apply_increase_block(desired, current_w, blocked)
    return fn


def make_h4_target(vol_gate: pd.Series):
    """vol_gate True means BTC σ20 > trailing median → block new/increased longs."""
    def fn(t, panel, pre, current_w, regime_state):
        row = pre["regime"].loc[t]
        state = vc_state_strict(row)
        desired = _vc_desired(t, panel, pre, current_w, state)
        if t in vol_gate.index and bool(vol_gate.loc[t]):
            blocked = set(desired.index)
            desired = apply_increase_block(desired, current_w, blocked)
        return desired
    return fn


def make_h5_target():
    def fn(t, panel, pre, current_w, regime_state):
        row = pre["regime"].loc[t]
        state = vc_state_soft(row)
        return _vc_desired(t, panel, pre, current_w, state)
    return fn


def btc_vol_gate(close_btc: pd.Series) -> pd.Series:
    lr = np.log(close_btc).diff()
    sig20 = lr.rolling(20, min_periods=20).std() * (365.0 ** 0.5)
    med = sig20.rolling(756, min_periods=252).median()
    gate = (sig20 > med) & med.notna()
    return gate.fillna(False)


def precompute_h3(panel: dict, symbols: list[str] | None = None) -> dict:
    symbols = symbols or ["BTC", "ETH"]
    core = core_signals(panel)
    extra = common_pre(panel, symbols)
    C = panel["close"]
    long = pd.DataFrame(False, index=C.index, columns=symbols)
    count = core["count"]
    above = core["above_sma200"]
    for s in symbols:
        if s not in C.columns or s not in count.columns:
            continue
        n_on = count[s].fillna(0).astype(int)
        ab = above[s].fillna(False)
        entry = ab & (n_on >= 2)
        exit_ = (~ab) | (n_on <= 0)
        pos = False
        flags = np.zeros(len(C.index), dtype=bool)
        for i in range(len(C.index)):
            if pos:
                if bool(exit_.iloc[i]):
                    pos = False
                    flags[i] = False
                else:
                    flags[i] = True
            else:
                if bool(entry.iloc[i]):
                    pos = True
                    flags[i] = True
        long[s] = flags
    extra.update({"long": long, "count": count, "above_sma200": above, "sma200": core["sma200"]})
    return extra


def make_h3_target(symbols: list[str]):
    def fn(t, panel, pre, current_w, regime_state):
        sig = pre["long"].loc[t] if t in pre["long"].index else pd.Series(False, index=symbols)
        w = vol_target_weights(
            t, pre, sig, current_w, regime_state, vol_target=P2.CORE_VOL_TARGET, symbols=list(symbols)
        )
        return clip_caps(w, name_cap=0.50, gross_cap=1.0)
    return fn


def is_weekly_signal(t) -> bool:
    """1Wutc week (Mon 00:00 UTC open) closes at the next Monday 00:00 UTC.

    That instant is the close of the Sunday 1Dutc bar (open Sunday 00:00).
    Signal on that Sunday close; fill at the next 1Dutc open (Monday).
    """
    return int(pd.Timestamp(t).weekday()) == 6


def precompute_h2(panel: dict, universe: pd.DataFrame) -> dict:
    pre = common_pre(panel, list(panel["close"].columns))
    C = panel["close"]
    ret20 = C / C.shift(20) - 1.0
    btc = ret20["BTC"] if "BTC" in ret20.columns else pd.Series(np.nan, index=C.index)
    excess = ret20.sub(btc, axis=0)
    pre.update({"U": universe, "ret20": ret20, "excess20": excess, "sma100": sma(C, 100)})
    return pre


def make_h2_target():
    last = {"w": None}

    def fn(t, panel, pre, current_w, regime_state):
        cols = list(panel["close"].columns)
        if not is_weekly_signal(t):
            if last["w"] is None:
                return pd.Series(0.0, index=cols)
            return last["w"].reindex(cols).fillna(0.0)
        w = pd.Series(0.0, index=cols)
        if t not in pre["U"].index:
            last["w"] = w
            return w
        members = [s for s in cols if bool(pre["U"].loc[t].get(s, False))]
        close = panel["close"].loc[t]
        s100 = pre["sma100"].loc[t] if t in pre["sma100"].index else pd.Series(dtype=float)
        ex = pre["excess20"].loc[t] if t in pre["excess20"].index else pd.Series(dtype=float)
        ranked = []
        for s in members:
            px = close.get(s, np.nan)
            sma100 = s100.get(s, np.nan) if hasattr(s100, "get") else np.nan
            r = ex.get(s, np.nan) if hasattr(ex, "get") else np.nan
            if pd.notna(px) and pd.notna(sma100) and pd.notna(r) and float(px) > float(sma100):
                ranked.append((s, float(r)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        pick = [s for s, _ in ranked[:5]]
        if len(pick) < 2:
            last["w"] = w
            return w
        raw = pd.Series(0.0, index=cols)
        for s in pick:
            raw[s] = 1.0 / len(pick)
        # vol target 12%, then 10% name cap and 100% gross, then regime on new risk
        w = scale_to_vol(t, pre, raw, current_w, "NORMAL", vol_target=P2.CORE_VOL_TARGET)
        w = clip_caps(w, name_cap=P1.NAME_CAP, gross_cap=P1.GROSS_CAP)
        # H2 rule text has no F1/F2; kill and daily-loss cap still come from the engine.
        last["w"] = w
        return w

    return fn


def params_for(hid: str) -> dict:
    common = {
        "brief": "strategy-v3-brief",
        "id": hid,
        "name": NAMES[hid],
        "cost_primary": 0.004,
        "vol_target": 0.12,
        "kill": "non-sticky -15% DD, re-arm 30 flat days AND BTC > SMA200",
        "daily_loss_cap": -0.02,
        "fill": "signal on closed 1Dutc t (confirm==1); fill at open of t+1",
        "capital": "C_paper required; 6788 wedding funds excluded",
    }
    extra = {
        "H1": {
            "base": "V-C",
            "filter": "block new/increased longs when proxy funding >= trailing 365d p90 and positive",
            "proxy": "BTC funding gates BTC and all alts; ETH funding gates ETH only",
            "stale": "skip filter if last print >3 calendar days before bar close",
            "funding_parse": "archive module 3 funding_time as UTC+8; REST fundingTime as UTC",
            "name_cap": 0.10,
            "regime": "V-C F2 HALT (no F1)",
        },
        "H2": {
            "base": "standalone",
            "universe": "PIT top-20 inside favorites.txt (v1 rule)",
            "rank": "20-session excess close-to-close return vs BTC on closed 1Wutc",
            "qualify": "1Dutc close > SMA100; flat if fewer than 2 names",
            "hold": "until next weekly review",
            "name_cap": 0.10,
            "vol_band": "20% only if membership unchanged",
            "regime": "F2 HALT + F1 REDUCE (macroy)",
        },
        "H3": {
            "base": "V2-CORE entry, slower exit (new code; V2-CORE frozen)",
            "universe": ["BTC", "ETH"],
            "entry": "close > SMA200 AND >=2 of 3 Donchian 20/60/90 long",
            "exit": "close <= SMA200 OR 0 of 3 Donchian still long",
            "name_cap": 0.50,
            "regime": "V2-CORE F2 HALT + F1 REDUCE",
        },
        "H4": {
            "base": "V-C",
            "filter": "block new/increased longs when BTC 20d vol > trailing 756d median (min_periods=252)",
            "name_cap": 0.10,
            "regime": "V-C F2 HALT (no F1)",
        },
        "H5": {
            "base": "V-C exact except F2 multiplier",
            "regime": "F2 is REDUCE 0.5 in [tau-24h, tau+24h); stale still HALT; no force-exit",
            "name_cap": 0.10,
        },
    }
    return {**common, **extra[hid]}


def build_hypotheses(
    panel: dict,
    *,
    regime: pd.DataFrame,
    crowd: pd.DataFrame | None,
    pit_universe: pd.DataFrame,
) -> dict[str, Hypothesis]:
    vc_pre_fn = lambda p: {**precompute_vc(p), "regime": regime}
    vol_gate = btc_vol_gate(panel["close"]["BTC"])
    hs = {
        "H1": Hypothesis(
            id="H1",
            name="FUND-CROWD",
            symbols=None,
            use_band=True,
            band_membership_only=False,
            describe="V-C plus funding crowding filter on new longs",
            precompute=vc_pre_fn,
            target_fn=make_h1_target(crowd if crowd is not None else pd.DataFrame()),
            params=params_for("H1"),
        ),
        "H2": Hypothesis(
            id="H2",
            name="RS-ROTATE",
            symbols=None,
            use_band=True,
            band_membership_only=True,
            describe="Weekly top-5 excess vs BTC, close>SMA100, 12% vol, 10% cap",
            precompute=lambda p, u=pit_universe: {**precompute_h2(p, u), "regime": regime},
            target_fn=make_h2_target(),
            params=params_for("H2"),
        ),
        "H3": Hypothesis(
            id="H3",
            name="CORE-SLOW",
            symbols=("BTC", "ETH"),
            use_band=False,
            band_membership_only=False,
            describe="V2-CORE entry; exit on close<=SMA200 or 0/3 Donchian",
            precompute=lambda p: {**precompute_h3(p, ["BTC", "ETH"]), "regime": regime},
            target_fn=make_h3_target(["BTC", "ETH"]),
            params=params_for("H3"),
        ),
        "H4": Hypothesis(
            id="H4",
            name="VOL-GATE",
            symbols=None,
            use_band=True,
            band_membership_only=False,
            describe="V-C plus BTC vol median gate on new longs",
            precompute=lambda p, g=vol_gate: {**precompute_vc(p), "regime": regime, "vol_gate": g},
            target_fn=make_h4_target(vol_gate),
            params=params_for("H4"),
        ),
        "H5": Hypothesis(
            id="H5",
            name="F2-SOFT",
            symbols=None,
            use_band=True,
            band_membership_only=False,
            describe="V-C with F2 HALT softened to REDUCE 0.5",
            precompute=vc_pre_fn,
            target_fn=make_h5_target(),
            params=params_for("H5"),
        ),
    }
    if set(hs) != set(HYPOTHESIS_IDS):
        raise RuntimeError("hypothesis set mismatch")
    return hs
