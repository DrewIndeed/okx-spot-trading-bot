"""v3 load (closed candles only), manifest checksums, PIT universe, frozen v2 untouched."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from tradingbot.data.store import save_candles
from tradingbot.data.v3_manifest import build_manifest, file_sha256, verify_manifest, write_manifest
from tradingbot.data.v3_universe import is_excluded, pit_topn, seed_bases
from tradingbot.eval.v3_load import load_closed, load_panel
from tradingbot.types import Candle

OFFICIAL = Path("/workspace/trading-bot")
from tradingbot.paths import REPO_ROOT as WORKTREE  # the code under test
FROZEN = [
    "tradingbot/strategies/spec_v2_common.py",
    "tradingbot/strategies/spec_v2_core.py",
    "tradingbot/eval/v2_engine.py",
    "tradingbot/strategies/spec_v1_common.py",
    "tradingbot/strategies/spec_v1_signals.py",
    "tradingbot/strategies/spec_v1_vc.py",
    "tradingbot/eval/engine.py",
]


def _candles(n=5, start_ms=1_700_000_000_000, step=86_400_000, last_open=False):
    out = []
    for i in range(n):
        ts = start_ms + i * step
        confirm = 0 if (last_open and i == n - 1) else 1
        out.append(Candle(ts=ts, open=10+i, high=11+i, low=9+i, close=10.5+i, volume_base=1, volume_quote=100+i, confirm=confirm))
    return out


def test_load_drops_unconfirmed_bars(tmp_path):
    save_candles(tmp_path, "BTC-USDT", "1Dutc", _candles(n=4, last_open=True), backend="parquet")
    df = load_closed("BTC-USDT", "1Dutc", cache_dir=tmp_path)
    assert len(df) == 3
    assert list(df.columns) == ["open", "high", "low", "close", "vq"]


def test_manifest_detects_tamper(tmp_path):
    save_candles(tmp_path, "BTC-USDT", "1Dutc", _candles(n=3), backend="parquet")
    man = build_manifest(tmp_path, bars=("1Dutc",))
    write_manifest(man, tmp_path / "manifest.json")
    assert verify_manifest(man, tmp_path) == []
    p = tmp_path / "BTC-USDT_1Dutc.parquet"
    p.write_bytes(p.read_bytes() + b"x")
    problems = verify_manifest(man, tmp_path)
    assert any("checksum mismatch" in x for x in problems)


def test_stables_and_wrapped_excluded_from_seed():
    bases = seed_bases(favorites=["BTC", "USDC", "WBTC", "ETH"], historical=["USDT", "DAI"], tickers=["BTC-USDT"])
    assert "BTC" in bases and "ETH" in bases
    assert "USDC" not in bases and "WBTC" not in bases and "USDT" not in bases
    assert is_excluded("USDC-USDT")
    assert not is_excluded("BTC-USDT")


def test_pit_topn_uses_prior_month_only():
    # Jan: AAA volume high; Feb: BBB volume high. February membership must still be AAA.
    idx = pd.date_range("2022-01-01", periods=90, freq="D")
    close = pd.DataFrame(
        {"AAA": 1.0, "BBB": 1.0, "BTC": 1.0},
        index=idx,
    )
    # 30d history needed: pad with 200 days before
    pre = pd.date_range("2021-06-01", periods=214, freq="D")
    close_pre = pd.DataFrame({"AAA": 1.0, "BBB": 1.0, "BTC": 1.0}, index=pre)
    close = pd.concat([close_pre, close]).sort_index()
    close = close[~close.index.duplicated(keep="last")]
    vq = close.copy()
    # Until end of January, AAA has huge volume; BBB tiny. Then swap in February.
    vq.loc[: "2022-01-31", "AAA"] = 5_000_000
    vq.loc[: "2022-01-31", "BBB"] = 10.0
    vq.loc[: "2022-01-31", "BTC"] = 4_000_000
    vq.loc["2022-02-01":, "AAA"] = 10.0
    vq.loc["2022-02-01":, "BBB"] = 9_000_000
    vq.loc["2022-02-01":, "BTC"] = 4_000_000
    U = pit_topn(close, vq, top_n=2, min_history=200, median_vol_min=2_000_000.0)
    # 2022-02-01 is recomputed on t-1 (Jan 31) → AAA and BTC, not BBB.
    feb1 = pd.Timestamp("2022-02-01")
    if feb1 in U.index:
        assert bool(U.loc[feb1, "AAA"]) is True
        assert bool(U.loc[feb1, "BBB"]) is False


@pytest.mark.skipif(not OFFICIAL.exists(), reason="box-only: official tree /workspace/trading-bot absent")
def test_frozen_v2_and_vc_sources_match_official_tree():
    for rel in FROZEN:
        a = (OFFICIAL / rel).read_bytes()
        b = (WORKTREE / rel).read_bytes()
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest(), rel
