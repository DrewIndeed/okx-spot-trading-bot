"""Tests for the official spec v3 paper-forward runner (TDD).

Paper / dry-run only. These tests must not start live trading, touch
/workspace/trading-bot, or read OKX private endpoints.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingbot.config import Config
from tradingbot.eval.v3_protocol import sha256_params, sha256_paths
from tradingbot.eval.v3_hypotheses import params_for


FREEZE = Path("/workspace/sprint/artifacts/spec_v3/freeze.json")
OFFICIAL_V3 = "spec_v3_official"


def _cfg(**kwargs):
    kw = dict(allocated_capital=10_000, c_paper=10_000)
    kw.update(kwargs)
    return Config(**kw)


def test_verify_freeze_matches_official_lock():
    from tradingbot.eval.v3_paper import verify_freeze

    info = verify_freeze(FREEZE)
    freeze = json.loads(FREEZE.read_text())
    assert info["ok"] is True
    assert info["sha256_v3_code"] == freeze["sha256_v3_code"] == sha256_paths()
    assert info["sha256_v3_code"].startswith("533ee52e")
    for hid in ("H1", "H4", "H5"):
        expected = next(c["sha256_params"] for c in freeze["candidates"] if c["id"] == hid)
        assert info["params"][hid] == expected == sha256_params(params_for(hid))
    assert info["forward_slots"] == ["H5", "H1", "H4"]


def test_verify_freeze_refuses_code_mismatch(monkeypatch):
    from tradingbot.eval import v3_paper as vp
    from tradingbot.eval.v3_paper import FreezeMismatch, verify_freeze

    monkeypatch.setattr(vp, "sha256_paths", lambda paths=None: "0" * 64)
    with pytest.raises(FreezeMismatch, match="code"):
        verify_freeze(FREEZE)


def test_verify_freeze_refuses_slotted_params_mismatch(monkeypatch):
    from tradingbot.eval import v3_paper as vp
    from tradingbot.eval.v3_paper import FreezeMismatch, verify_freeze

    monkeypatch.setattr(vp, "sha256_params", lambda p: "1" * 64)
    with pytest.raises(FreezeMismatch, match="H1|H4|H5|params"):
        verify_freeze(FREEZE)


def test_c_paper_official_is_10000_and_rejects_wedding_funds():
    from tradingbot.eval.v3_paper import c_paper_official

    assert c_paper_official(_cfg(c_paper=10_000)) == 10_000
    with pytest.raises(ValueError, match="6788|wedding"):
        c_paper_official(_cfg(c_paper=6788))
    with pytest.raises(ValueError, match="10000"):
        c_paper_official(_cfg(c_paper=5000))
    cfg = Config(allocated_capital=10_000, c_paper=None)
    with pytest.raises(ValueError, match="required"):
        c_paper_official(cfg)


def test_h1_missing_funding_turns_filter_off_and_logs_event():
    from tradingbot.eval.v3_paper import crowd_from_rest_or_off

    idx = pd.DatetimeIndex([pd.Timestamp("2026-11-10")])
    crowd, events = crowd_from_rest_or_off({}, idx, reason="rest_empty")
    assert any(e.get("type") == "h1_funding_missing" for e in events)
    assert all(e.get("action") == "filter_OFF" for e in events if e.get("type") == "h1_funding_missing")
    # make_h1_target skips the filter when t is absent from the crowd index,
    # or when skip flags are set. Do not invent a rate.
    if len(crowd) and idx[0] in crowd.index:
        row = crowd.loc[idx[0]]
        assert bool(row.get("btc_skip", True))
        assert bool(row.get("eth_skip", True))
        assert not bool(row.get("btc_crowd", False))
        assert not bool(row.get("eth_crowd", False))
    else:
        assert idx[0] not in crowd.index


def test_h1_paper_fetch_uses_only_rest_and_never_reads_archive(tmp_path):
    from tradingbot.eval.v3_paper import REST_FUNDING_URL, fetch_rest_funding_series

    archive = tmp_path / "funding"
    archive.mkdir()
    bait = archive / "BTC-USDT-SWAP-fundingrates-2024-01.zip"
    bait.write_bytes(b"this zip must never be opened")
    opened = []
    real_open = Path.open

    def spy_open(self, *a, **k):
        opened.append(str(self))
        return real_open(self, *a, **k)

    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": "0", "data": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        return _Resp()

    series, events = fetch_rest_funding_series(
        http_get=fake_get,
        dest_dir=archive,
        insts=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
    )
    assert calls, "REST funding-rate-history must be called"
    assert all(c["url"] == REST_FUNDING_URL for c in calls)
    assert all("funding-rate-history" in c["url"] for c in calls)
    assert not any("archive" in c["url"].lower() or "market-data-history" in c["url"] for c in calls)
    assert bait.read_bytes() == b"this zip must never be opened"
    assert not any(str(bait) == p for p in opened)
    # Empty REST payload → no series to trade on, events recorded, no backfill from the zip.
    assert all(s.empty for s in series.values()) or series == {}
    assert any("missing" in e.get("type", "") or "empty" in e.get("type", "") for e in events)


def test_v3_paths_stay_in_v3_worktree():
    from tradingbot.eval.v3_paper import LOCK_PATH, OFFICIAL_RUN_ID, run_dirs
    from tradingbot.paper_runner import pidfile_path, status_path

    dirs = run_dirs(OFFICIAL_RUN_ID)
    root = Path("/workspace/trading-bot-v3")
    official = Path("/workspace/trading-bot")
    live = Path("/workspace/trading-bot-live")
    for key in ("state", "logs", "pidfile"):
        p = Path(dirs[key])
        assert root in p.parents or p == root, key
        assert official not in p.parents and live not in p.parents
    assert Path(dirs["state"]) == root / "state" / "spec_v3" / OFFICIAL_RUN_ID
    assert Path(dirs["pidfile"]) == root / "state" / "spec_v3" / OFFICIAL_RUN_ID / "paper.pid"
    assert LOCK_PATH == root / "RUNNING.lock"
    pf = pidfile_path(run_id=OFFICIAL_RUN_ID)
    sf = status_path(run_id=OFFICIAL_RUN_ID)
    assert str(pf).startswith(str(root / "state" / "spec_v3"))
    assert str(sf).startswith(str(root / "state" / "spec_v3"))
    assert "spec_v2" not in str(pf)
    assert "/workspace/trading-bot/" not in str(pf)
    assert "/workspace/trading-bot-live" not in str(pf)


def test_waiting_for_t0_writes_section7_status_with_zero_errors(tmp_path, monkeypatch):
    from tradingbot.eval import v3_paper as vp

    monkeypatch.setattr(vp, "V3_ROOT", tmp_path)
    monkeypatch.setattr(vp, "ARTIFACT_ROOT", tmp_path / "artifacts")
    (tmp_path / "artifacts").mkdir()

    idx = pd.date_range("2026-10-01", "2026-11-10", freq="D")
    px = pd.DataFrame({"BTC": 100.0, "ETH": 10.0}, index=idx)
    panel = {
        "open": px.copy(),
        "high": px.copy(),
        "low": px.copy(),
        "close": px,
        "vq": px,
        "bases": ["BTC", "ETH"],
        "last_closed": idx[-1],
    }
    regime = pd.DataFrame(
        {"macroy_state": "NORMAL", "f2_halt": False, "f1_reduce": False, "btc_stale_halt": False},
        index=idx,
    )
    monkeypatch.setattr(vp, "load_events", lambda: pd.DataFrame({"tau": [pd.Timestamp("2026-11-14", tz="UTC")]}))
    monkeypatch.setattr(vp, "calendar_is_stale", lambda: False)
    monkeypatch.setattr(vp, "last_fetch_timestamp", lambda: pd.Timestamp("2026-11-11T10:00:00+07:00"))

    rc, summary = vp.run_v3_iteration(
        _cfg(),
        run_id=OFFICIAL_V3,
        official=True,
        update=False,
        panel=panel,
        regime=regime,
        funding_fetch=lambda **k: ({}, [{"type": "h1_funding_missing", "action": "filter_OFF", "reason": "test"}]),
        verify=True,
    )
    assert rc == 0
    assert summary["waiting_for_t0"] is True
    assert summary["n_errors"] == 0
    assert summary.get("errors") in (None, [],)
    status_md = (tmp_path / "artifacts" / "paper_status.md").read_text()
    status_js = json.loads((tmp_path / "artifacts" / "paper_status.json").read_text())
    assert "waiting for T0" in status_md.lower() or status_js["waiting_for_t0"] is True
    assert "2026-11-12 07:00 UTC+7" in status_md
    assert status_js["n_errors"] == 0
    assert status_js["t0_initialized"] is False
    for book in ("H5", "H1", "H4", "V-C", "BENCH-50-50"):
        assert book in status_js["books"]
        assert book in status_md
    # Bars before T0 are not counted: equities stay unset / em-dash, not a traded path.
    for book, row in status_js["books"].items():
        assert row["equity"] in (None, 10_000) or row.get("n_closed", 0) == 0
        assert (row.get("n_counted_bars") or 0) == 0
    assert "533ee52e" in status_md or "sha256_v3_code" in json.dumps(status_js)
    assert "not a strategy PASS" in status_md or "paper slots" in status_md.lower()


def test_start_background_refuses_on_freeze_mismatch_and_does_not_spawn(monkeypatch):
    from tradingbot.eval.v3_paper import FreezeMismatch
    from tradingbot import paper_runner as pr

    monkeypatch.setattr(pr, "verify_v3_freeze", lambda *a, **k: (_ for _ in ()).throw(FreezeMismatch("code hash mismatch")))
    spawned = []

    def fake_popen(*a, **k):
        spawned.append(a)
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(pr.subprocess, "Popen", fake_popen)
    pid, msg = pr.start_background(_cfg(), strategies="v3", run_id=OFFICIAL_V3, poll_sec=60)
    assert spawned == []
    assert pid in (0, None)
    assert "REFUSED" in msg
    assert "mismatch" in msg.lower()


def test_running_lock_payload_names_tobias_and_v3_purpose():
    from tradingbot.eval.v3_paper import running_lock_text

    text = running_lock_text(pid=4242, started="2026-11-11 17:30:00 +0700 (Asia/Saigon, UTC+7)")
    assert "owner:" in text and "Tobias" in text
    assert "pid: 4242" in text
    assert "spec_v3_official" in text
    assert "H5" in text and "H1" in text and "H4" in text
    assert "2026-11-12 07:00 UTC+7" in text
    assert "paper" in text.lower()
    assert "6788" in text or "wedding" in text.lower()


def test_cli_exposes_paper_start_status_stop_for_v3():
    from tradingbot.cli import build_parser

    p = build_parser()
    start = p.parse_args(["paper-start", "--strategies", "v3", "--run-id", OFFICIAL_V3, "--poll", "60"])
    assert start.cmd == "paper-start" and start.run_id == OFFICIAL_V3 and start.strategies == "v3"
    status = p.parse_args(["paper-status", "--run-id", OFFICIAL_V3])
    assert status.cmd == "paper-status" and status.run_id == OFFICIAL_V3
    stop = p.parse_args(["paper-stop", "--run-id", OFFICIAL_V3])
    assert stop.cmd == "paper-stop" and stop.run_id == OFFICIAL_V3
