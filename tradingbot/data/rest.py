"""OKX public REST candle feed with on-disk cache.

Fetch / throttle / history-backfill / parse logic reused from /workspace/crypto/data.py.
Public market data only — no API keys, no private endpoints, no account balance calls.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from tradingbot.types import Candle, Ticker
from tradingbot.data.store import existing_cache_file, load_candles, save_candles

DEFAULT_BASE = "https://www.okx.com/api/v5"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def parse_okx_rows(rows) -> list[list]:
    """[[ts, o, h, l, c, vol_base, vol_quote, confirm], ...] — same contract as crypto/data.py."""
    return [[int(r[0])] + [float(x) for x in r[1:7]] + [int(r[8])] for r in rows]


class OkxRestFeed:
    def __init__(
        self,
        cache_dir: str = "data/cache",
        rest_base: str = DEFAULT_BASE,
        timeout_sec: float = 10.0,
        session: Optional[requests.Session] = None,
        crypto_data_dir: str = "/workspace/crypto/data",
    ):
        self.cache_dir = Path(cache_dir)
        self.rest_base = rest_base.rstrip("/")
        self.timeout_sec = timeout_sec
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last = [0.0]
        # Read-only seed from the existing crypto cache. We never write back to this directory.
        self.crypto_data_dir = Path(crypto_data_dir) if crypto_data_dir else None

    def _cache_path(self, inst_id: str, bar: str) -> Path:
        """Legacy JSON path (kept for meta lookup / tests). Prefer store.existing_cache_file."""
        safe = inst_id.replace("/", "-")
        return self.cache_dir / f"{safe}_{bar}.json"

    def _meta_path(self, inst_id: str, bar: str) -> Path:
        safe = inst_id.replace("/", "-")
        return self.cache_dir / f"{safe}_{bar}_meta.json"

    def _seed_from_crypto(self, inst_id: str, bar: str) -> list[Candle]:
        """Copy-read /workspace/crypto/data/<SYM>_<bar>.json into our cache. Never writes to crypto/."""
        if not self.crypto_data_dir:
            return []
        base = inst_id.replace("-USDT", "").replace("-usdt", "")
        src = self.crypto_data_dir / f"{base}_{bar}.json"
        if not src.exists():
            return []
        rows = json.loads(src.read_text())
        candles = [Candle.from_row(r) for r in rows]
        if candles and existing_cache_file(self.cache_dir, inst_id, bar) is None:
            self._save(inst_id, bar, candles)
        return candles

    def load_cached(self, inst_id: str, bar: str) -> list[Candle]:
        have = load_candles(self.cache_dir, inst_id, bar)
        if have:
            return have
        return self._seed_from_crypto(inst_id, bar)

    def _save(self, inst_id: str, bar: str, candles: list[Candle]) -> None:
        save_candles(self.cache_dir, inst_id, bar, candles)

    def get(self, path: str, **q):
        """Throttled public GET. Reused pattern from /workspace/crypto/data.py (~12 req/s)."""
        for i in range(4):
            with self._lock:
                w = self._last[0] + 0.085 - time.time()
                if w > 0:
                    time.sleep(w)
                self._last[0] = time.time()
            try:
                r = self._session.get(
                    self.rest_base + path, params=q, headers=_HEADERS, timeout=self.timeout_sec
                )
                payload = r.json()
                if payload.get("code") == "0":
                    return payload["data"]
            except Exception:
                pass
            time.sleep(0.7 * (i + 1))
        return None

    def get_spot_tickers(self) -> dict[str, Ticker]:
        """One public call for every spot ticker. Keys are instId, e.g. BTC-USDT."""
        data = self.get("/market/tickers", instType="SPOT")
        out: dict[str, Ticker] = {}
        if not data:
            return out
        for row in data:
            inst = str(row.get("instId") or "")
            if not inst or row.get("last") in (None, ""):
                continue
            try:
                out[inst] = Ticker(
                    inst_id=inst,
                    last=float(row["last"]),
                    ts=int(row.get("ts") or 0),
                    bid=float(row["bidPx"]) if row.get("bidPx") else None,
                    ask=float(row["askPx"]) if row.get("askPx") else None,
                )
            except (TypeError, ValueError):
                continue
        return out

    def get_ticker(self, inst_id: str) -> Optional[Ticker]:
        data = self.get("/market/ticker", instId=inst_id)
        if not data:
            return None
        row = data[0]
        return Ticker(
            inst_id=inst_id,
            last=float(row["last"]),
            ts=int(row.get("ts") or 0),
            bid=float(row["bidPx"]) if row.get("bidPx") else None,
            ask=float(row["askPx"]) if row.get("askPx") else None,
        )

    def get_candles(
        self,
        inst_id: str,
        bar: str,
        *,
        days: Optional[int] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        update: bool = True,
        closed_only: bool = True,
    ) -> list[Candle]:
        have = {c.ts: c for c in self.load_cached(inst_id, bar)}
        if update:
            have = self._refresh(inst_id, bar, have, days=days or 365)
        candles = [have[k] for k in sorted(have)]
        # Apply days window to cached results too (not just REST refresh target)
        if days is not None and days > 0:
            cutoff = int(time.time() * 1000) - days * 86400000
            candles = [c for c in candles if c.ts >= cutoff]
        if start_ms is not None:
            candles = [c for c in candles if c.ts >= start_ms]
        if end_ms is not None:
            candles = [c for c in candles if c.ts <= end_ms]
        # Signal/sizing path uses closed candles only (data contract §2).
        if closed_only:
            candles = [c for c in candles if int(c.confirm) == 1]
        return candles

    def _refresh(self, inst_id: str, bar: str, have: dict[int, Candle], days: int) -> dict[int, Candle]:
        new = self.get("/market/candles", instId=inst_id, bar=bar, limit=300)
        if new is None:
            return have
        for row in parse_okx_rows(new):
            c = Candle.from_row(row)
            have[c.ts] = c
        target = int(time.time() * 1000) - days * 86400000
        oldest = min(have) if have else int(time.time() * 1000)
        meta_p = self._meta_path(inst_id, bar)
        listed = json.loads(meta_p.read_text()).get("listed") if meta_p.exists() else None
        while oldest > target and not (listed and oldest <= listed):
            page = self.get("/market/history-candles", instId=inst_id, bar=bar, limit=300, after=oldest)
            if not page:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                meta_p.write_text(json.dumps({"listed": oldest}))
                break
            for row in parse_okx_rows(page):
                c = Candle.from_row(row)
                have[c.ts] = c
            oldest = min(have)
        candles = [have[k] for k in sorted(have)]
        self._save(inst_id, bar, candles)
        return have
