"""Full historical candle pipeline for the favorites universe.

Incremental / resumable public REST download (same logic as /workspace/crypto/data.py via OkxRestFeed),
cached as parquet (CSV fallback). Writes data/quality_report.json and data/quality_report.md.

Never writes into /workspace/crypto. Public market data only. 1H is optional/secondary.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from tradingbot.config import Config
from tradingbot.data.quality import assess
from tradingbot.data.rest import OkxRestFeed
from tradingbot.data.store import BACKEND, cache_backend, existing_cache_file
from tradingbot.universe import inst_id, load_favorites

PRIMARY_BARS = ("4H", "1Dutc")
DEFAULT_DAYS = {"4H": 1800, "1Dutc": 1800, "1H": 400}


@dataclass
class SyncResult:
    backend: str
    bars: list[str]
    symbols: list[str]
    series: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    started_ts: float = 0.0
    finished_ts: float = 0.0
    report_json: Optional[str] = None
    report_md: Optional[str] = None


def _checkpoint_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "sync_checkpoint.json"


def _write_checkpoint(cache_dir: Path, rec: dict) -> None:
    p = _checkpoint_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = {}
    if p.exists():
        try:
            prev = json.loads(p.read_text())
        except Exception:
            prev = {}
    done = list(prev.get("completed") or [])
    key = f"{rec['symbol']}|{rec['bar']}"
    if key not in done:
        done.append(key)
    prev.update({"completed": done, "last": rec, "updated": time.time()})
    p.write_text(json.dumps(prev, indent=2))


def _ts_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def sync_universe(
    cfg: Config,
    *,
    bars: Iterable[str] = PRIMARY_BARS,
    days: Optional[int] = None,
    update: bool = True,
    include_1h: bool = False,
    report_dir: str | Path = "data",
    feed: Optional[OkxRestFeed] = None,
    progress: bool = True,
) -> SyncResult:
    bars = list(bars)
    if include_1h and "1H" not in bars:
        bars.append("1H")
    symbols = [inst_id(s) for s in load_favorites(cfg.universe_file)]
    cache_dir = Path(cfg.data.cache_dir)
    report_dir = Path(report_dir)
    backend = cache_backend()
    result = SyncResult(backend=backend, bars=bars, symbols=symbols, started_ts=time.time())
    feed = feed or OkxRestFeed(
        cache_dir=str(cache_dir),
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )
    n_jobs = len(symbols) * len(bars)
    i = 0
    for bar in bars:
        bar_days = days if days is not None else DEFAULT_DAYS.get(bar, 1800)
        for inst in symbols:
            i += 1
            try:
                candles = feed.get_candles(
                    inst, bar, days=bar_days, update=update, closed_only=False
                )
                q = assess(candles, bar, inst_id=inst)
                q["cache_file"] = str(existing_cache_file(cache_dir, inst, bar) or "")
                q["days_requested"] = bar_days
                result.series.append(q)
                _write_checkpoint(
                    cache_dir,
                    {"symbol": inst, "bar": bar, "n": q["n"], "ok": q["ok"], "issues": q["issues"]},
                )
                if progress:
                    flag = "OK" if q["ok"] else "ISSUES"
                    print(
                        f"[{i}/{n_jobs}] {inst} {bar}  n={q['n']} closed={q['n_closed']}  {flag}"
                        + (f"  {','.join(q['issues'])}" if q["issues"] else ""),
                        flush=True,
                    )
            except Exception as e:
                err = {"symbol": inst, "bar": bar, "error": str(e)}
                result.errors.append(err)
                if progress:
                    print(f"[{i}/{n_jobs}] {inst} {bar}  ERROR {e}", flush=True)
    result.finished_ts = time.time()
    report = write_quality_report(result, report_dir)
    result.report_json, result.report_md = report
    return result


def write_quality_report(result: SyncResult, report_dir: Path | str) -> tuple[str, str]:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    counts = [s["n_closed"] for s in result.series if s.get("n_closed") is not None]
    issue_series = [s for s in result.series if not s.get("ok")]
    warn_series = [s for s in result.series if s.get("warnings")]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone_note": "Bar timestamps are UTC; report clock times also as UTC+7 (Asia/Saigon) in the markdown.",
        "cache_format": result.backend,
        "cache_format_note": (
            "parquet via pyarrow"
            if result.backend == "parquet"
            else "CSV fallback (pyarrow not importable)"
        ),
        "bars": result.bars,
        "symbol_count": len(result.symbols),
        "symbols": result.symbols,
        "series_count": len(result.series),
        "elapsed_sec": round(result.finished_ts - result.started_ts, 1) if result.finished_ts else None,
        "summary": {
            "series_ok": sum(1 for s in result.series if s.get("ok")),
            "series_with_issues": len(issue_series),
            "series_with_warnings": len(warn_series),
            "fetch_errors": len(result.errors),
            "bar_count_closed_min": min(counts) if counts else 0,
            "bar_count_closed_max": max(counts) if counts else 0,
            "bar_count_closed_median": sorted(counts)[len(counts) // 2] if counts else 0,
        },
        "errors": result.errors,
        "series": result.series,
    }
    json_path = report_dir / "quality_report.json"
    md_path = report_dir / "quality_report.md"
    json_path.write_text(json.dumps(payload, indent=2))
    md_path.write_text(_render_md(payload))
    return str(json_path), str(md_path)


def _render_md(p: dict) -> str:
    s = p["summary"]
    lines = [
        "# Candle data quality report",
        "",
        f"Generated: {p['generated_at']} (UTC). Bar times are UTC; Asia/Saigon is UTC+7.",
        f"Cache format: **{p['cache_format']}** ({p['cache_format_note']}).",
        f"Universe: {p['symbol_count']} USDT-spot names from favorites.txt. Bars: {', '.join(p['bars'])}.",
        f"Series: {p['series_count']}. Closed-bar counts: min={s['bar_count_closed_min']}, "
        f"median={s['bar_count_closed_median']}, max={s['bar_count_closed_max']}.",
        f"OK: {s['series_ok']}. With issues: {s['series_with_issues']}. "
        f"With warnings: {s['series_with_warnings']}. Fetch errors: {s['fetch_errors']}.",
        "",
        "Checks: gaps (expected interval), duplicates, non-monotonic timestamps, "
        "NaN OHLCV, zero OHLC (error), zero volume (warning), 4H/1D alignment (warning).",
        "",
        "| Symbol | Bar | Closed | Total | First (UTC) | Last (UTC) | Issues | Warnings |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for row in p["series"]:
        lines.append(
            "| {symbol} | {bar} | {n_closed} | {n} | {first} | {last} | {issues} | {warns} |".format(
                symbol=row.get("symbol") or "",
                bar=row.get("bar") or "",
                n_closed=row.get("n_closed") or 0,
                n=row.get("n") or 0,
                first=_ts_iso(row.get("first_ts")),
                last=_ts_iso(row.get("last_ts")),
                issues=", ".join(row.get("issues") or []) or "—",
                warns=", ".join(row.get("warnings") or []) or "—",
            )
        )
    if p.get("errors"):
        lines += ["", "## Fetch errors", ""]
        for e in p["errors"]:
            lines.append(f"- {e.get('symbol')} {e.get('bar')}: {e.get('error')}")
    issue_rows = [r for r in p["series"] if r.get("issues")]
    if issue_rows:
        lines += ["", "## Series with issues", ""]
        for r in issue_rows:
            lines.append(
                f"- **{r['symbol']} {r['bar']}**: {', '.join(r['issues'])} "
                f"(gaps={r.get('gaps')}, dups={r.get('duplicates')}, "
                f"non_mono={r.get('non_monotonic')}, nan={r.get('nan_ohlcv')}, "
                f"zero_ohlc={r.get('zero_ohlc')})"
            )
    else:
        lines += ["", "No hard issues (gaps / duplicates / non-monotonic / NaN / zero OHLC / empty)."]
    lines.append("")
    return "\n".join(lines)
