"""v4 data: copy v3 1Dutc cache (read-only source), backfill older OKX public candles, fetch older funding archive.
Public endpoints only, no keys. Writes only under /workspace/trading-bot-v4/data."""
import json, shutil, sys, time
from pathlib import Path
import requests
from tradingbot.data.store import load_candles, save_candles
from tradingbot.types import Candle
from tradingbot.universe import load_favorites, inst_id
from tradingbot.eval import v3_funding as F

SRC = Path("/workspace/trading-bot-v3/data/cache")
V4 = Path("/workspace/trading-bot-v4/data")
CACHE = V4 / "cache"; FUND = V4 / "funding"
URL = "https://www.okx.com/api/v5/market/history-candles"

def backfill(sess, inst):
    have = load_candles(CACHE, inst, "1Dutc")
    if not have:
        return 0
    oldest = min(c.ts for c in have); new = []
    while True:
        for k in range(3):
            try:
                r = sess.get(URL, params={"instId": inst, "bar": "1Dutc", "after": str(oldest), "limit": "100"}, timeout=20).json(); break
            except Exception:
                time.sleep(1 + k)
        else:
            raise RuntimeError(f"fetch failed {inst}")
        rows = r.get("data") or []
        if r.get("code") != "0" or not rows:
            break
        for x in rows:
            new.append(Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[7]), int(x[8])))
        oldest = min(int(x[0]) for x in rows); time.sleep(0.15)
    if new:
        byts = {c.ts: c for c in new}; byts.update({c.ts: c for c in have})
        save_candles(CACHE, inst, "1Dutc", [byts[k] for k in sorted(byts)])
    return len(new)

def main():
    CACHE.mkdir(parents=True, exist_ok=True); FUND.mkdir(parents=True, exist_ok=True)
    for p in SRC.glob("*_1Dutc*"):
        if not (CACHE / p.name).exists():
            shutil.copy2(p, CACHE / p.name)
    for z in Path("/workspace/trading-bot-v3/data/funding").glob("*.zip"):
        if not (FUND / z.name).exists():
            shutil.copy2(z, FUND / z.name)
    sess = requests.Session(); sess.headers.update(F.UA); out = {}
    for b in load_favorites():
        out[b] = backfill(sess, inst_id(b))
    man = F.fetch_archive(dest_dir=FUND, start="2018-01-01", end="2021-12-31")
    rep = {"backfilled_bars": out, "funding_months": len(man.get("records", [])), "funding_errors": man.get("errors", [])[:5]}
    (V4 / "fetch_v4_report.json").write_text(json.dumps(rep, indent=1, default=str)); print(json.dumps({k: v for k, v in out.items() if v}), rep["funding_months"], rep["funding_errors"])

if __name__ == "__main__":
    sys.exit(main())
