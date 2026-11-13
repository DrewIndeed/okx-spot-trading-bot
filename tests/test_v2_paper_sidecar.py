"""Deviation D1: v2_paper mirrors refreshed 1Dutc candles into the JSON cache load_panel() reads."""

from __future__ import annotations

import json

import pandas as pd

from tradingbot.eval.load import load_closed_1dutc
from tradingbot.eval.v2_paper import _write_1dutc_json_sidecar
from tradingbot.types import Candle

DAY = 86_400_000
D11 = int(pd.Timestamp("2026-11-11", tz="UTC").value // 1_000_000)


def _c(ts: int, px: float, confirm: int = 1) -> Candle:
    return Candle(ts=ts, open=px, high=px + 1, low=px - 1, close=px + 0.5,
                  volume_base=1.0, volume_quote=100.0, confirm=confirm)


class _Feed:
    def __init__(self, candles):
        self.candles = candles

    def load_cached(self, inst_id, bar):
        assert bar == "1Dutc"
        return list(self.candles)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingbot.eval.load.TB_CACHE", tmp_path)
    monkeypatch.setattr("tradingbot.eval.load.CRYPTO_CACHE", tmp_path / "missing")


def test_sidecar_adds_fill_date_open_after_refresh(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # Stale JSON: stops at 2026-11-11 with 2026-11-12 still unconfirmed (the T0 KeyError case).
    stale = [_c(D11 - DAY, 9.0).as_row(), _c(D11, 10.0).as_row(), _c(D11 + DAY, 11.0, confirm=0).as_row()]
    (tmp_path / "FOO-USDT_1Dutc.json").write_text(json.dumps(stale))
    assert pd.Timestamp("2026-11-12") not in load_closed_1dutc("FOO").index

    # Refreshed cache (parquet side) has 11-12 closed and 11-13 still open; older history trimmed.
    feed = _Feed([_c(D11, 10.0), _c(D11 + DAY, 11.2), _c(D11 + 2 * DAY, 12.0, confirm=0)])
    path = _write_1dutc_json_sidecar(feed, "FOO-USDT")
    assert path == tmp_path / "FOO-USDT_1Dutc.json"
    assert not (tmp_path / "FOO-USDT_1Dutc.json.tmp").exists()

    df = load_closed_1dutc("FOO")
    assert pd.Timestamp("2026-11-12") in df.index  # fill-date open now available
    assert df.at[pd.Timestamp("2026-11-12"), "open"] == 11.2  # refreshed row wins
    assert pd.Timestamp("2026-11-13") not in df.index  # open bar still rejected
    assert pd.Timestamp("2026-11-10") in df.index  # merged: history never shrinks
    rows = json.loads(path.read_text())
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def test_sidecar_noop_when_cache_empty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert _write_1dutc_json_sidecar(_Feed([]), "FOO-USDT") is None
    assert not (tmp_path / "FOO-USDT_1Dutc.json").exists()
