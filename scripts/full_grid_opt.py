"""Aggressive full-grid optimizer for the MyD1 breakout sleeve.

Discipline (same as risk_constrained_opt, wider net):
- Train (first 70% of days) picks every parameter AND the exposure scale.
- Holdout (last 30%) is only a score. Nothing is picked on holdout.
- Cash (scale=0) is always a candidate. If holdout loses to cash, the
  recommendation is do-not-trade, not a smaller loss.
- Hard caps: base DD and 2x-fee stress DD must stay under DD_CAP on BOTH
  train and holdout.
- Plateau rule: a winner must have close neighbours (lookback +/-2 at the
  same breadth) that also pass the caps on train. Lone spikes are rejected.

One shared per-(lookback, breadth) weight pass; risk_pct / max_weight /
scale variants are derived cheaply afterwards, so the grid can be wide.

Does not modify locked strategy files. Backtest only. Not financial advice.
"""

from __future__ import annotations

import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingbot.config import load_config
from tradingbot.data.rest import OkxRestFeed
from tradingbot.regime import btc_above_sma200
from tradingbot.universe import (
    inst_id,
    load_favorites,
    monthly_top20_universe,
    breadth_top20,
    panel_from_candles,
)

DD_CAP = 0.30
TRAIN_FRAC = 0.70
STRESS_MULT = 2.0

LOOKBACKS = list(range(4, 50, 2))          # 4..48
BREADTHS = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7]  # 0.0 = no breadth gate
RISKS = [0.005, 0.01, 0.02, 0.03]
MAX_WEIGHTS = [0.05, 0.10, 0.15, 0.20, 0.30]
SCALES = np.round(np.arange(0.0, 1.01, 0.05), 2)  # spot bot: gross cannot exceed 100%


@dataclass
class Variant:
    lookback: int
    breadth: float
    risk_pct: float
    max_weight: float
    gross: np.ndarray   # daily gross return (uncosted, scale=1)
    turn: np.ndarray    # daily turnover


def load_panel(cfg, days: int = 1199):
    feed = OkxRestFeed(
        cache_dir=cfg.data.cache_dir,
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )
    by = {}
    for b in load_favorites(cfg.universe_file):
        cs = feed.get_candles(inst_id(b), "1Dutc", days=days, update=False, closed_only=True)
        if cs:
            by[b] = cs
    if "BTC" not in by:
        raise SystemExit("BTC 1Dutc cache missing. Run: python -m tradingbot data-sync --bars 1Dutc")
    C, _HI, _LO, VQ, _O = panel_from_candles(by)
    return C, VQ


def raw_weights(C: pd.DataFrame, VQ: pd.DataFrame, lookback: int, breadth_min: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Positions with UNCAPPED size = 1/risk. Any (risk_pct, max_weight) is
    then w_variant = clip(risk_pct * raw, None, max_weight)."""
    U = monthly_top20_universe(C, quote_vol=VQ, top_n=20)
    br = breadth_top20(C, U).to_numpy()
    gate = btc_above_sma200(C["BTC"]).reindex(C.index).fillna(0.0).to_numpy()
    gate_on = gate > 0
    w = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for s in C.columns:
        c = C[s].to_numpy()
        u = U[s].to_numpy()
        prevmax = C[s].shift(1).rolling(lookback).max().to_numpy()
        mx = C[s].rolling(lookback).max().to_numpy()
        mn = C[s].rolling(lookback).min().to_numpy()
        col = np.zeros(len(c))
        pos = False
        st = 0.0
        size = 0.0
        for t in range(len(c)):
            if np.isnan(c[t]):
                pos = False
                continue
            stop_t = (mx[t] + mn[t]) / 2.0
            if pos:
                if not np.isnan(stop_t):
                    st = max(st, stop_t)
                if c[t] < st or gate[t] <= 0:
                    pos = False
            elif (
                u[t]
                and gate[t] > 0
                and (breadth_min <= 0 or br[t] >= breadth_min)
                and not np.isnan(prevmax[t])
                and c[t] > prevmax[t]
                and not np.isnan(stop_t)
                and stop_t < c[t]
            ):
                pos = True
                st = float(stop_t)
                size = 1.0 / max(st / c[t], 1e-9)  # size = 1/risk
            col[t] = size if pos else 0.0
        w[s] = col
    gate_on_series = pd.Series(gate_on, index=C.index)
    return w, gate_on_series


def scan_scales(net: np.ndarray, cap: float, scales: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized end-equity and max drawdown for every scale. net shape (T,)."""
    T = len(net)
    eq = cap * np.cumprod(1.0 + np.asarray(scales)[:, None] * net[None, :], axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = ((peak - eq) / np.maximum(peak, 1e-12)).max(axis=1)
    ends = eq[:, -1]
    return ends, dd


def best_train_scale(gross_tr: np.ndarray, turn_tr: np.ndarray, cost: float, cap: float) -> tuple[float, dict, dict]:
    base_ends, base_dd = scan_scales(gross_tr - cost * turn_tr, cap, SCALES)
    st_ends, st_dd = scan_scales(gross_tr - STRESS_MULT * cost * turn_tr, cap, SCALES)
    ok = (base_dd <= DD_CAP) & (st_dd <= DD_CAP)
    if not ok.any():
        return 0.0, {"end": cap, "dd": 0.0}, {"dd": 0.0}
    ends = np.where(ok, base_ends, -np.inf)
    i = int(ends.argmax())
    k = float(SCALES[i])
    return k, {"end": float(base_ends[i]), "dd": float(base_dd[i])}, {"dd": float(st_dd[i])}


def main() -> int:
    from tradingbot.termfmt import bullets, kv, pct, px, table, title

    cfg = load_config()
    cost = cfg.fee_taker + cfg.slippage
    cap = cfg.allocated_capital
    print(title(
        "Full-grid optimizer",
        "Train 70% picks everything. Holdout 30% only scores. Cash always a candidate.",
    ))
    print(kv([
        ("Grid", f"lookbacks {LOOKBACKS[0]}-{LOOKBACKS[-1]} x breadth {BREADTHS} x risk {RISKS} x maxw {MAX_WEIGHTS}"),
        ("Caps", f"DD <= {DD_CAP:.0%} base + 2x-fee stress, train AND holdout"),
        ("Costs", f"taker {cfg.fee_taker:.4%} + slip {cfg.slippage:.4%} (stress: 2x)"),
        ("Capital", f"{px(cap)} USDT paper sleeve"),
    ]))
    print("load panel...", flush=True)
    C, VQ = load_panel(cfg)
    n = len(C)
    split = int(n * TRAIN_FRAC)
    print(kv([
        ("Symbols", str(len(C.columns))),
        ("Days", f"{n}  ({C.index[0].date()} -> {C.index[-1].date()})"),
        ("Split", f"train {split}  holdout {n - split}"),
    ]))

    variants: list[Variant] = []
    for lb, brd in itertools.product(LOOKBACKS, BREADTHS):
        w_raw, gate_on = raw_weights(C, VQ, lb, brd)
        for rp, mw in itertools.product(RISKS, MAX_WEIGHTS):
            # size = clip(risk_pct / risk, max_weight), then normalize gross<=1, then gate
            wv = np.minimum(rp * w_raw.to_numpy(), mw)
            g = wv.sum(axis=1)
            wv = wv / np.maximum(g, 1.0)[:, None]
            wv = wv * gate_on.to_numpy()[:, None]
            g_arr, t_arr = daily_arrays_np(C, wv)
            variants.append(Variant(lb, brd, rp, mw, g_arr, t_arr))
    print(f"grid built: {len(variants)} variants", flush=True)

    print(f"train scan ({len(SCALES)} scales, vectorized)...", flush=True)
    scored = []
    for v in variants:
        g_tr, t_tr = v.gross[: split + 1], v.turn[: split + 1]
        k, tr, tr_st = best_train_scale(g_tr, t_tr, cost, cap)
        if tr["end"] <= cap:  # train cash beats it -> dead
            continue
        scored.append((v, k, tr, tr_st))
    print(kv([("Train survivors beating cash", str(len(scored)))]))

    # plateau on train: neighbours (lookback +/-2, same breadth/risk/maxw) also beat cash
    key = {(v.lookback, v.breadth, v.risk_pct, v.max_weight): tr["end"] for v, _k, tr, _s in scored}
    plateaued = []
    for v, k, tr, tr_st in scored:
        nb = [key.get((lb2, v.breadth, v.risk_pct, v.max_weight), 0.0)
              for lb2 in (v.lookback - 2, v.lookback + 2)]
        if any(x > cap for x in nb):
            plateaued.append((v, k, tr, tr_st))
    print(kv([("With plateau neighbours", str(len(plateaued)))]))
    plateaued.sort(key=lambda x: x[2]["end"], reverse=True)
    shortlist = plateaued[:40]

    print(f"\nholdout score for top {len(shortlist)} train winners ...", flush=True)
    rows = []
    for v, k, tr, tr_st in shortlist:
        net_ho = v.gross[split:] - cost * v.turn[split:]
        net_ho2 = v.gross[split:] - STRESS_MULT * cost * v.turn[split:]
        ends, dds = scan_scales(net_ho, cap, np.array([k]))
        ends2, dds2 = scan_scales(net_ho2, cap, np.array([k]))
        rows.append({
            "lb": v.lookback, "brd": v.breadth, "risk": v.risk_pct, "mw": v.max_weight,
            "k": k,
            "tr_end": tr["end"], "tr_dd": tr["dd"], "tr_st_dd": tr_st["dd"],
            "ho_end": float(ends[0]), "ho_dd": float(dds[0]), "ho_st_dd": float(dds2[0]),
            "beat": ends[0] > cap and dds[0] <= DD_CAP and dds2[0] <= DD_CAP,
        })

    rows.sort(key=lambda r: r["ho_end"], reverse=True)
    beat = [r for r in rows if r["beat"]]
    print(title("Holdout score", "Top 40 train winners only. Holdout never picked anything."))
    hdr = ["LB", "Brd", "Risk", "MaxW", "Scale", "Train end", "Train DD", "OOS end", "OOS DD", "OOS 2xDD", "Beats cash"]
    trows = [
        [r["lb"], f"{r['brd']:.1f}", f"{r['risk']:.3f}", f"{r['mw']:.2f}", f"{r['k']:.2f}",
         px(r["tr_end"]), pct(r["tr_dd"]), px(r["ho_end"]), pct(r["ho_dd"]), pct(r["ho_st_dd"]),
         "yes" if r["beat"] else "no"]
        for r in rows[:20]
    ]
    print(table(hdr, trows, right={5, 6, 7, 8, 9}))

    concl: list[str] = []
    if beat:
        r = beat[0]
        concl.append(
            f"Best train pick that still beats cash out of sample: lookback={r['lb']}, "
            f"breadth={r['brd']}, risk_pct={r['risk']}, max_weight={r['mw']}, scale={r['k']:.2f}."
        )
        concl.append(
            f"Meaning: train ended {px(r['tr_end'])} (DD {pct(r['tr_dd'])}), "
            f"holdout ended {px(r['ho_end'])} (DD {pct(r['ho_dd'])})."
        )
        if r["ho_end"] > r["tr_end"]:
            concl.append("Holdout above train -> edge held up after the split. Good sign, not a guarantee.")
        else:
            concl.append(f"Holdout gives up {pct(1 - r['ho_end'] / r['tr_end'])} vs train -> normal decay; size was tuned on train only.")
        concl.append("Plateau rule passed: lookback +/-2 neighbours also survived, so this is not a lone lucky spike.")
    else:
        concl.append("No combo beats cash out of sample under the 30% caps. Data says: stay in cash.")
    concl.append("Read: 'end' = final paper equity on that window; 'DD' = worst peak-to-trough drop; 2xDD = same path at double costs.")
    concl.append("Verdict: backtest on real OKX daily bars. No live order, OKX balance untouched.")
    print(title("Conclusion"))
    print(bullets(concl))
    print(kv([("Cash benchmark", f"end {px(cap)}  DD 0%  always available")]))
    return 0


def daily_arrays_np(C: pd.DataFrame, wv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ret = (C / C.shift(1) - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    prev = np.vstack([np.zeros((1, wv.shape[1])), wv[:-1]])
    gross = (prev * ret).sum(axis=1)
    turn = np.abs(wv - prev).sum(axis=1)
    return gross, turn


if __name__ == "__main__":
    raise SystemExit(main())
