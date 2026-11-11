"""Official v3 protocol tests: screening window, gates, freeze, brief hash."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from tradingbot.eval import v3_protocol as P


def test_brief_hash_matches_cypheous_lock():
    assert P.assert_brief_hash() == P.BRIEF_SHA256_EXPECTED
    assert P.N_OFFICIAL == 10
    assert P.N_CONSERVATIVE == 45
    assert list(P.HYPOTHESIS_IDS) == ["H1", "H2", "H3", "H4", "H5"]


def test_refuse_scores_outside_screening_window():
    P.assert_scores_are_screening_only("2023-01-01", "2024-12-31")
    with pytest.raises(RuntimeError, match="2025"):
        P.assert_scores_are_screening_only("2023-01-01", "2025-03-18")
    with pytest.raises(RuntimeError, match="selection"):
        P.assert_scores_are_screening_only("2025-03-20", "2025-11-02")


def test_screening_gates():
    ok, reason = P.screening_pass("H1", 30, 0.1, -0.10)
    assert ok and reason == ""
    ok, reason = P.screening_pass("H3", 8, 0.1, -0.10)
    assert ok
    ok, reason = P.screening_pass("H3", 7, 0.1, -0.10)
    assert not ok and "n=" in reason
    ok, reason = P.screening_pass("H5", 40, 0.0, -0.10)
    assert not ok and "Sharpe" in reason
    ok, reason = P.screening_pass("H5", 40, 0.2, -0.15)
    assert not ok and "maxDD" in reason
    ok, reason = P.screening_pass("H5", 40, 0.2, -0.149)
    assert ok


def test_slot_allocation_ranking_and_no_fillers():
    rows = [
        {"id": "H1", "Sharpe": 0.4, "maxDD": -0.10, "screen_pass": True},
        {"id": "H2", "Sharpe": 0.9, "maxDD": -0.12, "screen_pass": True},
        {"id": "H3", "Sharpe": 0.9, "maxDD": -0.05, "screen_pass": True},
        {"id": "H4", "Sharpe": 0.2, "maxDD": -0.08, "screen_pass": False},
        {"id": "H5", "Sharpe": 0.7, "maxDD": -0.11, "screen_pass": True},
    ]
    slots = P.allocate_slots(rows)
    assert [s["id"] for s in slots] == ["H3", "H2", "H5"]  # Sharpe then shallower DD; H4 failed, not filled
    # fewer than 3 pass → do not fill leftover slots
    only = [r for r in rows if r["id"] in ("H4",)]  # none pass
    assert P.allocate_slots(only) == []


def test_freeze_refuses_overwrite(tmp_path):
    path = tmp_path / "freeze.json"
    P.write_freeze({"candidate_ids": []}, path=path)
    with pytest.raises(P.AlreadyFrozen):
        P.write_freeze({"candidate_ids": ["H1"]}, path=path)
