"""Path-dependent paper/backtest engine for spec v1.

Fills at the open of bar t+1, costs on traded notional, sticky daily-loss cap
and kill-switch on one continuous path. Capital is a required config value;
this module never reads an exchange balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradingbot.eval.metrics import Trade
from tradingbot.strategies import spec_v1_common as P
from tradingbot.strategies.spec_v1_signals import cov_ann_at


def _clip_name_and_gross(w: pd.Series) -> pd.Series:
    w = w.clip(lower=0.0, upper=P.NAME_CAP)
    s = float(w.sum())
    if s > P.GROSS_CAP and s > 0:
        w = w * (P.GROSS_CAP / s)
        w = w.clip(upper=P.NAME_CAP)
        s = float(w.sum())
        if s > P.GROSS_CAP and s > 0:
            w = w * (P.GROSS_CAP / s)
    return w


def _scale_port_vol(w: pd.Series, log_rets: pd.DataFrame, t, cap: float = P.VOL_TARGET) -> pd.Series:
    active = [s for s in w.index if float(w[s]) > 0]
    if len(active) < 1:
        return w
    cov = cov_ann_at(log_rets, t, active)
    if cov is None:
        # Conservative fallback: assume correlation = 1, using each name's vol_ann column
        # if cov window is short; skip scale.
        return w
    cov = cov.reindex(index=active, columns=active)
    # If some pairwise cov is NaN, use a diagonal + zeros off-diagonal for missing pairs
    cov = cov.fillna(0.0)
    wv = w[active].astype(float).values
    pred = float(np.sqrt(max(0.0, wv @ cov.values @ wv)))
    if pred > cap and pred > 0:
        w = w.copy()
        w[active] = w[active] * (cap / pred)
    return w


def _apply_regime_to_new_risk(desired: pd.Series, current: pd.Series, state: str) -> pd.Series:
    desired = desired.astype(float).fillna(0.0)
    current = current.reindex(desired.index).astype(float).fillna(0.0)
    if state == "HALT":
        return pd.Series(np.minimum(desired.values, current.values), index=desired.index)
    if state == "REDUCE":
        inc = np.maximum(desired.values - current.values, 0.0)
        held = np.minimum(desired.values, current.values)
        return pd.Series(held + 0.5 * inc, index=desired.index)
    return desired


def va_targets(t, panel, pre, regime_row, current_w, va_book: dict, allow_new: bool) -> pd.Series:
    """Event-driven D1-BASE targets (F2 applied by caller via allow_new / regime)."""
    C = panel["close"]
    cols = C.columns
    mid = pre["va"]["mid"]
    entry_flag = pre["va"]["entry_flag"]
    r1 = bool(regime_row["r1_on"])
    # Drop stale pending entries from a prior bar that never filled.
    for s in list(va_book):
        if va_book[s].get("pending"):
            va_book.pop(s, None)
    w = pd.Series(0.0, index=cols)
    for s in cols:
        px = C.at[t, s]
        st_mid = mid.at[t, s] if s in mid.columns else np.nan
        st = va_book.get(s)
        if st is not None and not st.get("pending"):
            if pd.notna(st_mid):
                st["stop"] = max(st["stop"], float(st_mid)) if st["stop"] is not None else float(st_mid)
            exit_stop = pd.notna(px) and st["stop"] is not None and float(px) < st["stop"]
            exit_r1 = not r1
            if exit_stop or exit_r1 or pd.isna(px):
                va_book.pop(s, None)
                w[s] = 0.0
            else:
                w[s] = float(st["size"])
            continue
        if (
            allow_new
            and r1
            and bool(entry_flag.at[t, s])
            and pd.notna(px)
            and pd.notna(st_mid)
            and float(st_mid) < float(px)
        ):
            risk = (float(px) - float(st_mid)) / float(px)
            if risk <= 0 or not np.isfinite(risk):
                continue
            size = min(P.RISK_PCT / risk, P.NAME_CAP)
            w[s] = size
            va_book[s] = {"stop": float(st_mid), "size": float(size), "pending": True, "risk": float(risk)}
    w = _clip_name_and_gross(w)
    # Keep book sizes in sync with post-cap weights; drop zeros.
    for s in list(va_book):
        if float(w.get(s, 0.0)) <= 1e-12:
            va_book.pop(s, None)
        else:
            va_book[s]["size"] = float(w[s])
    return w


def vb_targets(t, panel, pre, current_w, allow_new: bool) -> pd.Series:
    C = panel["close"]
    sig = pre["vb"]["combined"].loc[t]
    stop = pre["vb"]["stop"].loc[t]
    vol = pre["vol"].loc[t]
    w = pd.Series(0.0, index=C.columns)
    for s in C.columns:
        if not bool(sig.get(s, False)):
            continue
        px = C.at[t, s]
        sig_vol = vol.get(s, np.nan)
        st = stop.get(s, np.nan)
        if pd.isna(px) or pd.isna(sig_vol) or sig_vol <= 0:
            continue
        if pd.isna(st) or st >= px:
            continue  # invalid stop → skip entry (spec §4.3)
        risk = (float(px) - float(st)) / float(px)
        if risk <= 0 or not np.isfinite(risk):
            continue
        w_vol = P.VOL_TARGET / float(sig_vol)
        w_risk = P.RISK_PCT / risk
        w[s] = min(w_vol, w_risk, P.NAME_CAP)
    w = _clip_name_and_gross(w)
    w = _scale_port_vol(w, pre["log_rets"], t)
    w = _clip_name_and_gross(w)
    return w


def vc_targets(t, panel, pre, current_w, allow_new: bool) -> pd.Series:
    C = panel["close"]
    vol = pre["vol"].loc[t]
    w = pd.Series(0.0, index=C.columns)
    for n in P.VC_DONCHIAN:
        long = pre["vc"]["sleeve_long"][n].loc[t]
        stop = pre["vc"]["sleeve_stop"][n].loc[t]
        for s in C.columns:
            if not bool(long.get(s, False)):
                continue
            px = C.at[t, s]
            sig_vol = vol.get(s, np.nan)
            st = stop.get(s, np.nan)
            if pd.isna(px) or pd.isna(sig_vol) or sig_vol <= 0:
                continue
            if pd.isna(st) or st >= px:
                continue
            risk = (float(px) - float(st)) / float(px)
            if risk <= 0 or not np.isfinite(risk):
                continue
            w_vol = P.VC_SLEEVE_VOL / float(sig_vol)
            w_risk = P.RISK_PCT / risk
            w[s] = w[s] + min(w_vol, w_risk, P.NAME_CAP)
    w = _clip_name_and_gross(w)
    w = _scale_port_vol(w, pre["log_rets"], t)
    w = _clip_name_and_gross(w)
    return w


def _signal_mask(variant: str, t, pre) -> pd.Series:
    if variant == "V-A":
        return pre["va"]["entry_flag"].loc[t].astype(int)
    if variant == "V-B":
        return pre["vb"]["combined"].loc[t].astype(int)
    any_long = pre["vc"]["any_long"].loc[t].astype(int)
    return any_long


def compute_desired(variant, t, panel, pre, regime_df, current_w, va_book, f2_for_va: bool) -> tuple[pd.Series, pd.Series, str]:
    row = regime_df.loc[t]
    if variant == "V-A":
        state = "HALT" if bool(row["f2_halt"]) else "NORMAL"
        allow_new = state != "HALT"
        desired = va_targets(t, panel, pre, row, current_w, va_book, allow_new=allow_new)
        # va_targets already skipped new entries when not allow_new; still clamp vs current for safety
        desired = _apply_regime_to_new_risk(desired, current_w, state)
        sig = (desired > 0).astype(int)
        return desired, sig, state
    if variant == "V-B":
        state = str(row["macroy_state"])  # F2 HALT + F1 REDUCE + stale HALT
        desired = vb_targets(t, panel, pre, current_w, allow_new=state != "HALT")
        desired = _apply_regime_to_new_risk(desired, current_w, state)
        sig = pre["vb"]["combined"].loc[t].astype(int)
        return desired, sig, state
    # V-C: F2 only
    state = "HALT" if (bool(row["f2_halt"]) or bool(row["btc_stale_halt"])) else "NORMAL"
    desired = vc_targets(t, panel, pre, current_w, allow_new=state != "HALT")
    desired = _apply_regime_to_new_risk(desired, current_w, state)
    sig = pre["vc"]["any_long"].loc[t].astype(int)
    return desired, sig, state


@dataclass
class EngineState:
    cash: float
    qty: pd.Series
    peak: float
    killed: bool
    kill_pending: bool
    block_new: bool
    va_book: dict
    last_sig: pd.Series
    open_trades: dict
    last_px: pd.Series
    eq: float
    pending: pd.Series
    pending_sig: pd.Series
    pending_signal_date: pd.Timestamp
    closed_trades: list
    gaps: list
    killed_on: pd.Timestamp | None
    regime_rows: list


@dataclass
class EngineResult:
    equity: pd.Series
    rets: pd.Series
    exposure: pd.Series
    turnover: pd.Series
    trades: list[Trade]
    killed_on: pd.Timestamp | None
    gaps: list[dict]
    variant: str
    cost_label: str
    cost: float
    capital: float
    last_closed: pd.Timestamp
    regime_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    end_state: EngineState | None = None


def run_path(
    variant: str,
    cost_label: str,
    panel: dict,
    pre: dict,
    regime_df: pd.DataFrame,
    capital: float,
    path_start: pd.Timestamp,
    path_end: pd.Timestamp,
    use_band: bool | None = None,
    disable_f2: bool = False,
    resume: EngineState | None = None,
) -> EngineResult:
    if capital is None or float(capital) <= 0:
        raise ValueError("capital (allocated_capital) is required and must be positive; never inferred.")
    C0 = float(capital)
    cost = P.COST_LEVELS[cost_label]
    if use_band is None:
        use_band = variant in ("V-B", "V-C")

    close, opens = panel["close"], panel["open"]
    cols = list(close.columns)
    dates = close.index[(close.index >= path_start) & (close.index <= path_end)]
    if len(dates) < 2:
        raise RuntimeError(f"Not enough dates in [{path_start.date()}, {path_end.date()}]")

    # First targets: last warmup close before path_start
    pre_dates = close.index[close.index < path_start]
    if len(pre_dates) == 0:
        raise RuntimeError("No warmup bar before path start; cannot form a t+1 fill.")

    qty = pd.Series(0.0, index=cols)
    cash = C0
    eq_prev = C0
    peak = C0
    killed = False
    kill_pending = False
    block_new = False
    va_book: dict = {}
    last_sig = pd.Series(0, index=cols, dtype=int)
    open_trades: dict[str, Trade] = {}
    closed_trades: list[Trade] = []
    gaps: list[dict] = []
    killed_on = None
    last_px = close.iloc[0].copy()
    pending_from_resume = None

    eq_s, r_s, ex_s, to_s = {}, {}, {}, {}
    regime_rows = []

    def current_weights_at(px: pd.Series, equity: float) -> pd.Series:
        if equity <= 0:
            return pd.Series(0.0, index=cols)
        return (qty * px.reindex(cols).fillna(0.0)) / equity

    def stop_for(s, t) -> float | None:
        if variant == "V-A":
            st = va_book.get(s)
            return None if st is None else st.get("stop")
        if variant == "V-B":
            v = pre["vb"]["stop"].at[t, s] if s in pre["vb"]["stop"].columns else np.nan
            return None if pd.isna(v) else float(v)
        # tightest sleeve stop among long sleeves, else any
        best = None
        for n in P.VC_DONCHIAN:
            if bool(pre["vc"]["sleeve_long"][n].at[t, s]):
                v = pre["vc"]["sleeve_stop"][n].at[t, s]
                if pd.notna(v):
                    best = v if best is None else max(best, float(v))
        return None if best is None else float(best)

    # Initial desired from last warmup close
    t0 = pre_dates[-1]
    px0 = close.loc[t0]
    cur0 = current_weights_at(px0, C0)
    # Optionally disable F2 for the raw D1 comparator
    regime_use = regime_df
    if disable_f2:
        regime_use = regime_df.copy()
        regime_use["f2_halt"] = False
        # recompute macroy without F2 (F1 / stale still apply for V-B)
        state = pd.Series("NORMAL", index=regime_use.index)
        state = state.mask(regime_use["f1_reduce"], "REDUCE")
        state = state.mask(regime_use["btc_stale_halt"], "HALT")
        regime_use["macroy_state"] = state

    pending, pending_sig, pstate = compute_desired(
        variant, t0, panel, pre, regime_use, cur0, va_book, f2_for_va=not disable_f2
    )
    pending_signal_date = t0
    if resume is not None:
        cash = float(resume.cash)
        qty = resume.qty.reindex(cols).fillna(0.0)
        peak = float(resume.peak)
        killed = bool(resume.killed)
        kill_pending = bool(resume.kill_pending)
        block_new = bool(resume.block_new)
        va_book = dict(resume.va_book)
        last_sig = resume.last_sig.reindex(cols).fillna(0).astype(int)
        open_trades = dict(resume.open_trades)
        last_px = resume.last_px.reindex(cols)
        eq_prev = float(resume.eq)
        pending = resume.pending.reindex(cols).fillna(0.0)
        pending_sig = resume.pending_sig.reindex(cols).fillna(0).astype(int)
        pending_signal_date = resume.pending_signal_date
        closed_trades = list(resume.closed_trades)
        gaps = list(resume.gaps)
        killed_on = resume.killed_on

    for dt in dates:
        o = opens.loc[dt]
        c = close.loc[dt]

        if killed and float(qty.abs().sum()) == 0.0:
            target = pd.Series(0.0, index=cols)
        elif kill_pending or killed:
            target = pd.Series(0.0, index=cols)
            killed = True
            if killed_on is None:
                killed_on = dt
            kill_pending = False
        else:
            target = pending.copy()

        # 20% vol-only band (V-B / V-C). Signal flips, stops, caps, kill always trade.
        if use_band and not killed and not kill_pending:
            cur_open_w = current_weights_at(o.fillna(0.0), eq_prev)
            for s in cols:
                if pending_sig.get(s, 0) == last_sig.get(s, 0) and int(pending_sig.get(s, 0)) != 0:
                    w_cur = float(cur_open_w[s])
                    w_tgt = float(target[s])
                    denom = max(w_cur, 0.01)
                    if abs(w_tgt - w_cur) / denom < P.VOL_BAND:
                        target[s] = w_cur

        # Daily-loss cap: block new entries this session
        if block_new and not killed:
            cur_open_w = current_weights_at(o.fillna(0.0), eq_prev)
            for s in cols:
                w_cur = float(cur_open_w[s])
                if w_cur <= 1e-12 and float(target[s]) > 0:
                    target[s] = 0.0
                    # drop pending V-A book for blocked new entries
                    if variant == "V-A" and s in va_book and va_book[s].get("pending"):
                        va_book.pop(s, None)
                elif float(target[s]) > w_cur + 1e-12:
                    target[s] = w_cur
        block_new = False

        traded_notional = 0.0
        # Execute toward target at open
        for s in cols:
            ox = o.get(s, np.nan) if hasattr(o, "get") else o[s]
            try:
                ox = o[s]
            except Exception:
                ox = np.nan
            w_tgt = float(target[s])
            if pd.isna(ox) or ox <= 0:
                w_cur = 0.0 if eq_prev <= 0 else float(qty[s] * (0.0 if pd.isna(last_px[s]) else last_px[s]) / eq_prev)
                if abs(w_tgt - w_cur) > 1e-9:
                    gaps.append({"date": str(pd.Timestamp(dt).date()), "symbol": s, "reason": "missing_open_skip_fill"})
                continue
            w_cur = 0.0 if eq_prev <= 0 else float(qty[s] * ox / eq_prev)
            desired_qty = (w_tgt * eq_prev) / ox
            dq = desired_qty - float(qty[s])
            notional = abs(dq) * ox
            if notional < 1e-8:
                continue
            if dq > 0:
                fill = ox * (1.0 + cost)
                cash -= dq * fill
                # opening or add
                if s not in open_trades and float(qty[s]) <= 1e-12:
                    st = stop_for(s, pending_signal_date)
                    # For V-A pending book, take stored stop/risk
                    risk = None
                    if variant == "V-A" and s in va_book:
                        st = va_book[s]["stop"]
                        risk = va_book[s].get("risk")
                        va_book[s]["pending"] = False
                    elif st is not None and st < ox:
                        risk = (ox - st) / ox
                    open_trades[s] = Trade(
                        variant=variant,
                        symbol=s,
                        entry_date=pd.Timestamp(dt),
                        exit_date=None,
                        entry_px=fill,
                        exit_px=None,
                        stop_at_entry=st,
                        cost_per_side=cost,
                        reason="open",
                    )
                    # stash risk on the object via dict side
                    open_trades[s].r_multiple = risk  # temporarily store risk frac
            else:
                fill = ox * (1.0 - cost)
                cash -= dq * fill  # dq < 0
                if desired_qty <= 1e-12 and s in open_trades:
                    tr = open_trades.pop(s)
                    risk_frac = tr.r_multiple if tr.r_multiple is not None and tr.r_multiple > 0 else None
                    # net return on name notional: exit_fill/entry_fill - 1
                    pnl = fill / tr.entry_px - 1.0
                    r_mult = None
                    if risk_frac:
                        r_mult = pnl / risk_frac
                    tr.exit_date = pd.Timestamp(dt)
                    tr.exit_px = fill
                    tr.pnl_frac = float(pnl)
                    tr.r_multiple = None if r_mult is None else float(r_mult)
                    tr.reason = "kill" if killed else "exit"
                    closed_trades.append(tr)
                    if variant == "V-A":
                        va_book.pop(s, None)
            qty[s] = float(qty[s] + dq)
            traded_notional += notional

        # Mark to close
        pos_val = 0.0
        for s in cols:
            px = c[s]
            if pd.isna(px):
                px = o[s] if pd.notna(o[s]) else last_px[s]
                if pd.isna(c[s]):
                    gaps.append({"date": str(pd.Timestamp(dt).date()), "symbol": s, "reason": "missing_close_mark"})
            if pd.notna(px):
                last_px[s] = px
                pos_val += float(qty[s]) * float(px)
        eq = cash + pos_val
        r = (eq / eq_prev - 1.0) if eq_prev > 0 else 0.0
        eq_s[dt] = eq
        r_s[dt] = r
        ex_s[dt] = (pos_val / eq) if eq else 0.0
        to_s[dt] = (traded_notional / eq_prev) if eq_prev else 0.0

        if eq > peak:
            peak = eq
        dd = (eq / peak - 1.0) if peak > 0 else 0.0
        if dd <= P.KILL_SWITCH_DD:
            kill_pending = True
        if r <= P.DAILY_LOSS_CAP:
            block_new = True

        eq_prev = eq
        last_sig = (qty > 1e-12).astype(int)

        regime_rows.append(
            {
                "date": str(pd.Timestamp(dt).date()),
                "state": (str(regime_use.loc[dt, "macroy_state"]) if variant == "V-B" else (
                    "HALT" if bool(regime_use.loc[dt, "f2_halt"]) else "NORMAL"
                )),
                "f2_halt": bool(regime_use.loc[dt, "f2_halt"]),
                "f1_reduce": bool(regime_use.loc[dt, "f1_reduce"]),
                "r1_on": bool(regime_use.loc[dt, "r1_on"]),
                "killed": bool(killed or kill_pending),
                "equity": eq,
            }
        )

        if killed:
            pending = pd.Series(0.0, index=cols)
            pending_sig = pd.Series(0, index=cols, dtype=int)
            continue

        cur_close_w = current_weights_at(c.fillna(last_px), eq)
        pending, pending_sig, pstate = compute_desired(
            variant, dt, panel, pre, regime_use, cur_close_w, va_book, f2_for_va=not disable_f2
        )
        pending_signal_date = dt

    equity = pd.Series(eq_s, name="equity").sort_index()
    rets = pd.Series(r_s, name="ret").sort_index()
    exposure = pd.Series(ex_s).sort_index()
    turnover = pd.Series(to_s).sort_index()
    end_state = EngineState(
        cash=float(cash),
        qty=qty.copy(),
        peak=float(peak),
        killed=bool(killed),
        kill_pending=bool(kill_pending),
        block_new=bool(block_new),
        va_book=va_book,
        last_sig=last_sig.copy(),
        open_trades=dict(open_trades),
        last_px=last_px.copy(),
        eq=float(eq_prev),
        pending=pending.copy(),
        pending_sig=pending_sig.copy(),
        pending_signal_date=pending_signal_date,
        closed_trades=list(closed_trades),
        gaps=list(gaps),
        killed_on=killed_on,
        regime_rows=list(regime_rows),
    )
    return EngineResult(
        equity=equity,
        rets=rets,
        exposure=exposure,
        turnover=turnover,
        trades=closed_trades + list(open_trades.values()),
        killed_on=killed_on,
        gaps=gaps,
        variant=variant,
        cost_label=cost_label,
        cost=cost,
        capital=C0,
        last_closed=pd.Timestamp(dates[-1]),
        regime_log=pd.DataFrame(regime_rows),
        end_state=end_state,
    )


def btc_buy_and_hold(
    panel: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    capital: float,
    cost: float = P.COST_LEVELS["1x"],
) -> tuple[pd.Series, pd.Series]:
    """One entry at the first open of the window with 1× costs, held to window end (no exit cost)."""
    o, c = panel["open"]["BTC"], panel["close"]["BTC"]
    dates = c.index[(c.index >= start) & (c.index <= end)]
    if len(dates) == 0:
        raise RuntimeError("No BTC bars in B&H window.")
    first = dates[0]
    ox = o.loc[first]
    if pd.isna(ox) or ox <= 0:
        raise RuntimeError("BTC open missing on B&H entry date.")
    fill = float(ox) * (1.0 + cost)
    qty = float(capital) / fill
    eq = {}
    for dt in dates:
        px = c.loc[dt]
        if pd.isna(px):
            px = o.loc[dt] if pd.notna(o.loc[dt]) else np.nan
        eq[dt] = qty * float(px) if pd.notna(px) else np.nan
    equity = pd.Series(eq).sort_index().ffill()
    rets = equity.pct_change()
    # first day's return from starting capital C to first close
    rets.iloc[0] = equity.iloc[0] / float(capital) - 1.0
    return equity, rets
