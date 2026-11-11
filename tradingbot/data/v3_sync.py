"""Expand the v3 candle cache from OKX public REST only.

Writes under /workspace/trading-bot-v3/data/ (and artifacts/spec_v3). Never writes
to /workspace/trading-bot or /workspace/crypto.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from tradingbot.config import load_config
from tradingbot.data.quality import assess
from tradingbot.data.rest import OkxRestFeed
from tradingbot.data.store import existing_cache_file
from tradingbot.data.v3_manifest import build_manifest, write_manifest
from tradingbot.data.v3_universe import is_excluded, seed_inst_ids
from tradingbot.universe import load_favorites, inst_id

ROOT = Path("/workspace/trading-bot-v3")
CACHE = ROOT / "data" / "cache"
LOCAL_DATA = ROOT / "data"
ARTIFACT = Path("/workspace/sprint/artifacts/spec_v3")
DEFAULT_DAYS = {"1Dutc": 2800, "4H": 2800, "1H": 1200}
TOP_TICKER_N = 40


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_top_usdt_tickers(feed: OkxRestFeed, n: int = TOP_TICKER_N) -> list[str]:
    data = feed.get("/market/tickers", instType="SPOT") or []
    rows = []
    for row in data:
        inst = str(row.get("instId") or "")
        if not inst.endswith("-USDT") or is_excluded(inst):
            continue
        try:
            vol = float(row.get("volCcy24h") or row.get("volCcyQuote24h") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        rows.append((inst, vol))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [inst for inst, _ in rows[:n]]


def fetch_spot_usdt_instruments(feed: OkxRestFeed) -> list[str]:
    data = feed.get("/public/instruments", instType="SPOT") or []
    out = []
    for row in data:
        inst = str(row.get("instId") or "")
        if inst.endswith("-USDT") and not is_excluded(inst) and row.get("state") in (None, "live"):
            out.append(inst)
    return out


def sync_series(feed: OkxRestFeed, inst: str, bar: str, days: int, update: bool = True) -> dict:
    candles = feed.get_candles(inst, bar, days=days, update=update, closed_only=False)
    q = assess(candles, bar, inst_id=inst)
    q["cache_file"] = str(existing_cache_file(feed.cache_dir, inst, bar) or "")
    q["days_requested"] = days
    return q


def run(
    *,
    cache_dir: Path = CACHE,
    bars: tuple[str, ...] = ("1Dutc", "4H", "1H"),
    days: dict[str, int] | None = None,
    update: bool = True,
    top_n: int = TOP_TICKER_N,
) -> dict:
    days = days or dict(DEFAULT_DAYS)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(ROOT / "config.yaml"))
    feed = OkxRestFeed(
        cache_dir=str(cache_dir),
        rest_base=cfg.data.rest_base,
        timeout_sec=max(cfg.data.timeout_sec, 15.0),
        crypto_data_dir=cfg.crypto_data_dir,
    )
    started = time.time()
    print(f"[{_utc_now()}] fetching OKX public tickers + instruments (no keys)", flush=True)
    tickers = fetch_top_usdt_tickers(feed, n=top_n)
    # instruments list is used only to validate seed names, not to pull every long-tail coin
    live = set(fetch_spot_usdt_instruments(feed))
    insts = seed_inst_ids(tickers=tickers)
    if live:
        insts = [i for i in insts if i in live] or insts
    seed_path = LOCAL_DATA / "universe_seed.txt"
    seed_path.write_text(" ".join(insts) + "\n")
    print(f"seed n={len(insts)} tickers_top{top_n}={len(tickers)} live_usdt={len(live)}", flush=True)

    series: list[dict] = []
    errors: list[dict] = []
    jobs = [(bar, inst) for bar in bars for inst in insts]
    for i, (bar, inst) in enumerate(jobs, 1):
        bar_days = int(days.get(bar, DEFAULT_DAYS.get(bar, 1800)))
        try:
            q = sync_series(feed, inst, bar, bar_days, update=update)
            series.append(q)
            flag = "OK" if q.get("ok") else "ISSUES"
            print(
                f"[{i}/{len(jobs)}] {inst} {bar} n={q.get('n')} closed={q.get('n_closed')} {flag}",
                flush=True,
            )
        except Exception as e:
            err = {"symbol": inst, "bar": bar, "error": str(e)}
            errors.append(err)
            print(f"[{i}/{len(jobs)}] {inst} {bar} ERROR {e}", flush=True)

    elapsed = time.time() - started
    extra = {
        "seed_path": str(seed_path),
        "seed": insts,
        "top_tickers": tickers,
        "n_live_usdt_spot": len(live),
        "elapsed_sec": round(elapsed, 1),
        "errors": errors,
        "survivorship_note": (
            "PIT top-N is computed only inside this seed (favorites + historical majors "
            "+ OKX USDT-spot top-volume snapshot at download). Delisted names never in "
            "the seed are missing. Residual survivorship bias is not zero."
        ),
    }
    manifest = build_manifest(cache_dir, bars=bars, extra=extra)
    # Canonical copy only: /workspace/sprint/artifacts/spec_v3/data/manifest.json
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / "data").mkdir(parents=True, exist_ok=True)
    man_art = write_manifest(manifest, ARTIFACT / "data" / "manifest.json")
    man_local = man_art

    # compact quality report
    quality = {
        "generated_at_utc": _utc_now(),
        "elapsed_sec": round(elapsed, 1),
        "n_instruments": len(insts),
        "bars": list(bars),
        "n_series": len(series),
        "n_errors": len(errors),
        "n_ok": sum(1 for s in series if s.get("ok")),
        "n_with_issues": sum(1 for s in series if s.get("issues")),
        "n_with_warnings": sum(1 for s in series if s.get("warnings")),
        "errors": errors,
        "series": series,
    }
    (LOCAL_DATA / "quality_report_v3.json").write_text(json.dumps(quality, indent=2, default=str))
    (ARTIFACT / "data" / "quality_report_v3.json").write_text(json.dumps(quality, indent=2, default=str))
    lines = [
        "# v3 candle quality report",
        "",
        f"Generated: {quality['generated_at_utc']} (UTC). Times are UTC; Asia/Saigon is UTC+7.",
        f"Seed instruments: {len(insts)}. Bars: {', '.join(bars)}. Series: {len(series)}.",
        f"OK: {quality['n_ok']}. Issues: {quality['n_with_issues']}. "
        f"Warnings: {quality['n_with_warnings']}. Fetch errors: {len(errors)}.",
        f"Elapsed: {quality['elapsed_sec']}s. Manifest: {man_art}",
        "",
        extra["survivorship_note"],
        "",
        "| Symbol | Bar | Closed | Total | First (UTC) | Last (UTC) | Issues | Warnings |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for row in series:
        first = row.get("first_ts")
        last = row.get("last_ts")
        def iso(ms):
            if not ms:
                return ""
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"| {row.get('symbol','')} | {row.get('bar','')} | {row.get('n_closed') or 0} | "
            f"{row.get('n') or 0} | {iso(first)} | {iso(last)} | "
            f"{', '.join(row.get('issues') or []) or '—'} | "
            f"{', '.join(row.get('warnings') or []) or '—'} |"
        )
    if errors:
        lines += ["", "## Fetch errors", ""]
        for e in errors:
            lines.append(f"- {e.get('symbol')} {e.get('bar')}: {e.get('error')}")
    md = "\n".join(lines) + "\n"
    (LOCAL_DATA / "quality_report_v3.md").write_text(md)
    (ARTIFACT / "data" / "quality_report_v3.md").write_text(md)
    print(f"manifest {man_art}  elapsed={elapsed:.1f}s", flush=True)
    return {"manifest": str(man_art), "quality": str(ARTIFACT / "data" / "quality_report_v3.md"), "errors": errors}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="v3 OKX public candle sync (paper/research only)")
    p.add_argument("--skip-1h", action="store_true")
    p.add_argument("--days-1d", type=int, default=DEFAULT_DAYS["1Dutc"])
    p.add_argument("--days-4h", type=int, default=DEFAULT_DAYS["4H"])
    p.add_argument("--days-1h", type=int, default=DEFAULT_DAYS["1H"])
    p.add_argument("--no-update", action="store_true")
    args = p.parse_args(argv)
    bars = ("1Dutc", "4H") + (() if args.skip_1h else ("1H",))
    days = {"1Dutc": args.days_1d, "4H": args.days_4h, "1H": args.days_1h}
    run(bars=bars, days=days, update=not args.no_update)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
