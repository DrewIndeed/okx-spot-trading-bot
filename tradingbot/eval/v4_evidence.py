"""Spec v4 SUPPORTING evidence for the FROZEN V4-COMBO (eval-only; frozen files are imported, never edited).

Pre-registration: /workspace/sprint/artifacts/spec_v4/evidence/prereg.json (sha256 checked before running).
Phases: fetch (Binance public dumps -> data/pit_cache), run (all four tests exactly once; refuses if results exist).
Paper/backtest only: public data, no keys, no orders.
"""
from __future__ import annotations

import argparse, csv, hashlib, io, json, re, sys, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from tradingbot.eval import v3_engine as V3
from tradingbot.eval import v4_exits as V4X

EVAL_ROOT = Path(__file__).resolve().parents[2]
EV = Path("/workspace/sprint/artifacts/spec_v4/evidence")
PREREG_SHA = "9c3c379c3610b5b8250a37dc2aaa448aa5cd3fed4292df121f638e5ef7667621"
MAJORS = {"BTC", "ETH"}
BASE_FILL = V3._fill_to_weights
STABLES = {"USDC", "TUSD", "PAX", "USDP", "USDS", "USDSB", "BUSD", "DAI", "UST", "SUSD", "FDUSD", "EUR", "GBP", "AUD",
           "TRY", "RUB", "BIDR", "IDRT", "BKRW", "NGN", "UAH", "BRL", "ZAR", "PLN", "RON", "ARS"}
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DL = "https://data.binance.vision/"
COST_2X = 0.004
SLIPS = {"2x": 0.0, "slip25": 0.0025, "slip50": 0.0050}
BOOKS = ["V4-COMBO", "H5", "V2-CORE", "BENCH-50-50"]
HO = (pd.Timestamp("2018-11-01"), pd.Timestamp("2020-09-30"))
WF = (pd.Timestamp("2021-07-01"), pd.Timestamp("2026-11-10"))


# ---------- tested helpers ----------
def make_slip_fill(extra: float):
    """Frozen fill, split into {BTC,ETH} at `cost` and alts at `cost+extra`. Mutates st exactly like the frozen fill."""
    def fill(st, target_w, open_px, date, cost, *, block_new, flatten):
        full, traded = st.qty, 0.0
        for grp, c in ((lambda s: s in MAJORS, cost), (lambda s: s not in MAJORS, cost + extra)):
            st.qty = {s: q for s, q in full.items() if grp(s)}
            tw = {s: w for s, w in target_w.items() if grp(s)}
            traded += BASE_FILL(st, tw, open_px, date, c, block_new=block_new, flatten=flatten)
            full.update(st.qty)
        st.qty = full
        return traded
    return fill


class slippage:
    def __init__(self, extra): self.f = make_slip_fill(extra)
    def __enter__(self):
        V3._fill_to_weights, V4X._ORIG_FILL = self.f, self.f
    def __exit__(self, *a):
        V3._fill_to_weights, V4X._ORIG_FILL = BASE_FILL, BASE_FILL


def _sh(a):
    sd = a.std(axis=-1, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        s = a.mean(axis=-1) / sd * np.sqrt(365)
    return np.where(sd > 0, s, 0.0)


def bootstrap_sharpe_gap(ra: pd.Series, rb: pd.Series, *, block=20, n=10000, seed=20261112) -> dict:
    """Paired stationary block bootstrap (Politis-Romano) of Sharpe(a)-Sharpe(b)."""
    df = pd.concat([ra, rb], axis=1).dropna()
    a, b = df.iloc[:, 0].to_numpy(), df.iloc[:, 1].to_numpy()
    T, p = len(a), 1.0 / block
    rng = np.random.default_rng(seed)
    gaps = []
    for lo in range(0, n, 1000):
        m = min(1000, n - lo)
        idx = np.empty((m, T), dtype=np.int64)
        idx[:, 0] = rng.integers(0, T, m)
        new = rng.random((m, T)) < p
        jump = rng.integers(0, T, (m, T))
        for t in range(1, T):
            idx[:, t] = np.where(new[:, t], jump[:, t], (idx[:, t - 1] + 1) % T)
        gaps.append(_sh(a[idx]) - _sh(b[idx]))
    g = np.concatenate(gaps)
    obs = float(_sh(a) - _sh(b))
    return {"gap": obs, "ci95": [float(np.percentile(g, 2.5)), float(np.percentile(g, 97.5))],
            "p_one_sided": float((1 + (g <= 0).sum()) / (1 + n)), "n_days": T, "block": block, "B": n, "seed": seed}


def rolling_windows(start, end, months=12, step=3):
    s, e, out = pd.Timestamp(start), pd.Timestamp(end), []
    while True:
        we = s + pd.DateOffset(months=months) - pd.Timedelta(days=1)
        if we > e:
            return out
        out.append((s, we))
        s = s + pd.DateOffset(months=step)


def keep_symbol(sym: str):
    if not sym.endswith("USDT"):
        return None
    b = sym[:-4]
    if not b or b in STABLES or (len(b) > 4 and re.search(r"(UP|DOWN|BULL|BEAR)$", b)):
        return None
    return b


def klines_to_frame(rows, cutoff="2020-09-30") -> pd.DataFrame:
    lim = int(pd.Timestamp(cutoff).value // 10**6) + 86_400_000 - 1
    recs = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
             "volume_base": float(r[5]), "volume_quote": float(r[7]), "confirm": 1} for r in rows if int(r[0]) <= lim]
    return pd.DataFrame(recs, columns=["ts", "open", "high", "low", "close", "volume_base", "volume_quote", "confirm"])


# ---------- fetch ----------
def _get(url, tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r
        except Exception:
            if i == tries - 1:
                raise
    raise RuntimeError(f"GET failed {url}")


def _list(prefix, delim=True):
    keys, marker = [], ""
    while True:
        x = _get(f"{S3}?prefix={prefix}&marker={marker}" + ("&delimiter=/" if delim else "")).text
        got = re.findall(r"<Prefix>([^<]+)</Prefix>", x)[1:] if delim else re.findall(r"<Key>([^<]+)</Key>", x)
        keys += got
        if "<IsTruncated>true" not in x or not got:
            return keys
        marker = got[-1]


def phase_fetch():
    out = EVAL_ROOT / "data" / "pit_cache"
    out.mkdir(parents=True, exist_ok=True)
    syms = [p.rstrip("/").split("/")[-1] for p in _list("data/spot/monthly/klines/")]
    cands = [s for s in syms if keep_symbol(s)]

    def months(s):
        ks = [k for k in _list(f"data/spot/monthly/klines/{s}/1d/", delim=False) if k.endswith(".zip")]
        ym = sorted(k[-11:-4] for k in ks)
        return s, ks, ym

    with ThreadPoolExecutor(16) as ex:
        listing = list(ex.map(months, cands))
    manifest = []

    def load(item):
        s, ks, ym = item
        use = [k for k in ks if "2018-01" <= k[-11:-4] <= "2020-09"]
        if not use:
            return None
        rows = []
        for k in sorted(use):
            z = zipfile.ZipFile(io.BytesIO(_get(DL + k).content))
            rows += [r for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))) if r and r[0].isdigit()]
        df = klines_to_frame(rows).drop_duplicates("ts").sort_values("ts")
        b = keep_symbol(s)
        df.to_parquet(out / f"{b}-USDT_1Dutc.parquet", index=False)
        return {"symbol": s, "base": b, "first": str(pd.to_datetime(df.ts.min(), unit="ms").date()),
                "last_used": str(pd.to_datetime(df.ts.max(), unit="ms").date()), "rows": len(df),
                "last_month_in_archive": ym[-1], "delisted_or_renamed": ym[-1] < "2026-10"}

    with ThreadPoolExecutor(16) as ex:
        manifest = [m for m in ex.map(load, listing) if m]
    (out / "manifest.json").write_text(json.dumps({"source": "data.binance.vision monthly 1d klines", "n_bucket_symbols": len(syms),
                                                   "n_usdt_candidates": len(cands), "symbols": manifest}, indent=1))
    print("PIT symbols", len(manifest), "delisted/renamed", sum(m["delisted_or_renamed"] for m in manifest))


# ---------- run ----------
def verify_freeze():
    from tradingbot.eval import v4_paper as P
    f = json.loads(P.FREEZE_PATH.read_text())
    r = P.verify_freeze()
    for p, h in f["v4_code_file_sha256"].items():
        ep = EVAL_ROOT / Path(p).relative_to("/workspace/trading-bot-v4")
        if hashlib.sha256(ep.read_bytes()).hexdigest() != h:
            sys.exit(f"REFUSE: eval copy {ep} differs from freeze")
    if hashlib.sha256((EV / "prereg.json").read_bytes()).hexdigest() != PREREG_SHA:
        sys.exit("REFUSE: prereg.json changed")
    return r


def ctx_for(cache, bases):
    from tradingbot.eval.regime import build_regime
    from tradingbot.eval.v3_load import load_panel
    from tradingbot.universe import monthly_top20_universe
    panel = load_panel(cache, bases=bases, bar="1Dutc")
    return panel, build_regime(panel), monthly_top20_universe(panel["close"], panel["vq"]), None


def stats(eq, rets, trades, expo, turn):
    from tradingbot.eval.v3_metrics import summarize
    m = summarize(eq, rets, trades, expo, turn)
    return {k: m.get(k) for k in ("Sharpe", "CAGR", "maxDD", "n_closed_trades")}


def phase_run():
    from tradingbot.eval.run_spec_v4 import run_book
    from tradingbot.universe import load_favorites
    if (EV / "results.csv").exists():
        sys.exit("REFUSE: evidence already run once")
    fz = verify_freeze()
    print("freeze ok", fz["sha256_v4_code"][:8], fz["sha256_params"][:8], flush=True)
    stored = {(r["book"], r["cost"]): r for r in json.loads(Path("/workspace/sprint/artifacts/spec_v4/holdout_results.json").read_text())["rows"]}
    orig = ctx_for(EVAL_ROOT / "data" / "cache", load_favorites())
    pc = EVAL_ROOT / "data" / "pit_cache"
    man = json.loads((pc / "manifest.json").read_text())
    pit = ctx_for(pc, [m["base"] for m in man["symbols"]])
    rows, raw = [], {}
    for uname, ctx in (("holdout", orig), ("PIT", pit)):
        for cl, x in SLIPS.items():
            for b in BOOKS:
                with slippage(x):
                    res = run_book(b, ctx, *HO, COST_2X)
                raw[(uname, cl, b)] = res
                m = stats(*res)
                if uname == "holdout" and cl == "2x" and (b, "2x") in stored:
                    s = stored[(b, "2x")]
                    if abs(s["Sharpe"] - m["Sharpe"]) > 1e-9 or abs(s["CAGR"] - m["CAGR"]) > 1e-9:
                        sys.exit(f"REFUSE: holdout 2x not reproduced for {b}: {s['Sharpe']} vs {m['Sharpe']}")
                rows.append({"test": "T1/T2", "universe": uname, "cost": cl, "book": b, **m})
                print(uname, cl, b, {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()}, flush=True)
    # PIT diagnostics
    U = pit[2].loc[HO[0]:HO[1]]
    ever = sorted(U.columns[U.any()])
    dl = {m["base"] for m in man["symbols"] if m["delisted_or_renamed"]}
    last = {m["base"]: pd.Timestamp(m["last_used"]) for m in man["symbols"]}
    diag = {"n_pit_symbols": len(man["symbols"]), "n_delisted_or_renamed": len(dl), "ever_in_top20": len(ever),
            "ever_in_top20_delisted": sorted(set(ever) & dl)}
    for b in ("V4-COMBO", "H5"):
        tr = raw[("PIT", "2x", b)][2]
        diag[f"{b}_trades_on_delisted"] = sum(1 for t in tr if t.get("symbol") in dl)
        diag[f"{b}_open_at_series_end"] = [t["symbol"] for t in tr if not t.get("exit_date") and last.get(t["symbol"], HO[1]) < HO[1]]
        diag[f"{b}_symbols_traded"] = sorted({t.get("symbol") for t in tr})
    # WF period + bootstrap + rolling
    wf = {}
    for b in ("V4-COMBO", "H5", "V2-CORE"):
        with slippage(0.0):
            wf[b] = run_book(b, orig, *WF, COST_2X)
        rows.append({"test": "T4-full", "universe": "orig", "cost": "2x", "book": b, "window": "2021-07-01..2026-11-10", **stats(*wf[b])})
    boot = {"holdout": bootstrap_sharpe_gap(raw[("holdout", "2x", "V4-COMBO")][1], raw[("holdout", "2x", "H5")][1]),
            "wf_2021-07..2026-11": bootstrap_sharpe_gap(wf["V4-COMBO"][1], wf["H5"][1])}
    for k, v in boot.items():
        rows.append({"test": "T3", "universe": "orig", "cost": "2x", "book": "COMBO-H5", "window": k, "Sharpe": v["gap"],
                     "ci_lo": v["ci95"][0], "ci_hi": v["ci95"][1], "p_one_sided": v["p_one_sided"]})
    win = {"H5": 0, "V2-CORE": 0}
    wins = rolling_windows(*WF)
    for s, e in wins:
        sh = {b: float(_sh(wf[b][1].loc[s:e].dropna().to_numpy())) for b in wf}
        for o in win:
            win[o] += sh["V4-COMBO"] > sh[o]
        for b in wf:
            rows.append({"test": "T4", "universe": "orig", "cost": "2x", "book": b, "window": f"{s.date()}..{e.date()}", "Sharpe": sh[b]})
    pd.DataFrame(rows).to_csv(EV / "results.csv", index=False)
    (EV / "results_extra.json").write_text(json.dumps({"bootstrap": boot, "rolling_wins": win, "n_windows": len(wins), "pit_diag": diag,
                                                       "freeze": fz, "prereg_sha256": PREREG_SHA}, indent=1, default=str))
    print(json.dumps({"bootstrap": boot, "wins": win, "n": len(wins), "diag": diag}, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["fetch", "run"])
    {"fetch": phase_fetch, "run": phase_run}[ap.parse_args().phase]()
