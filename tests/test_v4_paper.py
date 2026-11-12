"""spec_v4_shadow paper runner: freeze refuse-on-mismatch, T0 window, C_paper."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from tradingbot.eval import v4_paper as V


def _freeze(tmp_path):
    p = tmp_path / "freeze_v4.json"
    V.write_freeze(p, frozen_at="test")
    return p


def test_write_freeze_is_write_once_and_verifies(tmp_path):
    p = _freeze(tmp_path)
    info = V.verify_freeze(p)
    assert info["ok"] and info["sha256_v4_code"] == V.code_hash()
    assert info["sha256_params"] == V.params_hash()
    with pytest.raises(FileExistsError):
        V.write_freeze(p, frozen_at="again")


def test_freeze_lists_combo_overlay_and_frozen_files(tmp_path):
    f = json.loads(_freeze(tmp_path).read_text())
    assert f["candidate"] == "V4-COMBO" and f["run_id"] == "spec_v4_shadow"
    assert f["params"]["overlay"]["trail_atr"] == 3.0 and f["params"]["overlay"]["vol_shock"] == 1.5
    assert f["params"]["overlay"]["buy_trend"] is True
    assert any(x.endswith("v4_exits.py") for x in f["v4_code_paths"])
    assert f["refuse_on_mismatch"] is True and "no more v4 tuning" in f["notes"].lower()


def test_verify_refuses_code_mismatch(tmp_path):
    p = _freeze(tmp_path)
    f = json.loads(p.read_text()); f["sha256_v4_code"] = "0" * 64; p.write_text(json.dumps(f))
    with pytest.raises(V.FreezeMismatch):
        V.verify_freeze(p)


def test_verify_refuses_params_mismatch(tmp_path):
    p = _freeze(tmp_path)
    f = json.loads(p.read_text()); f["sha256_params"] = "1" * 64; p.write_text(json.dumps(f))
    with pytest.raises(V.FreezeMismatch):
        V.verify_freeze(p)


def test_start_refuses_on_mismatch_and_does_not_spawn(tmp_path, monkeypatch):
    p = _freeze(tmp_path)
    f = json.loads(p.read_text()); f["sha256_v4_code"] = "0" * 64; p.write_text(json.dumps(f))
    spawned = []
    monkeypatch.setattr(V.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(V.FreezeMismatch):
        V.start(freeze_path=p, root=tmp_path)
    assert spawned == [] and not (tmp_path / "RUNNING.lock").exists()


def test_window_counts_nothing_before_first_fill_after_t0():
    t0 = V.T0_BAR
    assert V.counted_window(t0 - pd.Timedelta(days=1)) is None  # T0 bar not closed yet
    assert V.counted_window(t0) is None                          # armed only, no fill bar closed
    s, e = V.counted_window(t0 + pd.Timedelta(days=3))
    assert s == t0 + pd.Timedelta(days=1) and e == t0 + pd.Timedelta(days=3)


def test_t0_is_first_1dutc_close_after_freeze():
    # bar opened 2026-11-12 00:00 UTC closes 2026-11-13 07:00 UTC+7
    assert V.T0_BAR == pd.Timestamp("2026-11-12")


def test_c_paper_is_10000_and_never_wedding_funds():
    assert V.C_PAPER == 10000.0 and V.C_PAPER != 6788.0
    assert V.COST == 0.004


def test_forward_books_start_at_c_paper_and_exclude_pre_t0(monkeypatch):
    ctx = V.load_ctx()
    last = ctx[0]["close"].index[-1]
    monkeypatch.setattr(V, "T0_BAR", last - pd.Timedelta(days=5))
    books = V.forward_books(ctx, last)
    for name in ("V4-COMBO", "BENCH-50-50"):
        eq = books[name]["equity"]
        assert pd.Timestamp(eq.index[0]) == V.T0_BAR + pd.Timedelta(days=1)
        assert len(eq) == 5
