"""Official v3 screening protocol (strategy-v3-brief). No historical final check."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

SAIGON = ZoneInfo("Asia/Saigon")
ARTIFACT = Path("/workspace/sprint/artifacts/spec_v3")
BRIEF_PATH = Path("/workspace/sprint/strategy-v3-brief.md")
BRIEF_SHA256_EXPECTED = "6544095cf2e7203935e9741f3bfaef139afc3fddee0676f3c6b364f7dbbd72d2"
LOCK_PATH = ARTIFACT / "freeze.json"
C_PAPER = 10000.0
N_OFFICIAL = 10  # 3 v1 + 2 v2 + 5 this brief
N_CONSERVATIVE = 45  # 10 + 35 reversal trials (report only, not a gate)
DSR_GATE_FORWARD = 0.95  # T+180 forward only; not a screening gate
SCREEN_START = pd.Timestamp("2023-01-01")
SCREEN_END = pd.Timestamp("2024-12-31")
COST_PRIMARY = 0.0040  # 2x = 40 bps/side
COST_REPORT = {
    "1x": 0.0020,
    "25bps": 0.0025,
    "2x": 0.0040,
    "50bps": 0.0050,
}
MIN_TRADES = {"H1": 30, "H2": 30, "H3": 8, "H4": 30, "H5": 30}
MAX_DD_FLOOR = -0.15  # must be strictly better (shallower) than -15%
MAX_SLOTS = 3
HYPOTHESIS_IDS = ("H1", "H2", "H3", "H4", "H5")
SANITY_START = pd.Timestamp("2023-01-01")
SANITY_END = pd.Timestamp("2026-11-10")

V3_CODE_PATHS = [
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_protocol.py"),
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_engine.py"),
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_metrics.py"),
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_hypotheses.py"),
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_funding.py"),
    Path("/workspace/trading-bot-v3/tradingbot/eval/v3_load.py"),
]


class AlreadyFrozen(RuntimeError):
    pass


def ts_saigon(now: datetime | None = None) -> str:
    now = now or datetime.now(SAIGON)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SAIGON)
    return now.strftime("%Y-%m-%d %H:%M:%S %z") + " (Asia/Saigon, UTC+7)"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_paths(paths: list[Path] | None = None) -> str:
    h = hashlib.sha256()
    for p in sorted(paths or V3_CODE_PATHS, key=lambda x: str(x)):
        p = Path(p)
        h.update(str(p).encode())
        h.update(p.read_bytes() if p.exists() else b"MISSING")
    return h.hexdigest()


def sha256_params(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def assert_brief_hash() -> str:
    got = sha256_file(BRIEF_PATH)
    if got != BRIEF_SHA256_EXPECTED:
        raise RuntimeError(
            f"strategy-v3-brief.md hash {got} != frozen {BRIEF_SHA256_EXPECTED}; refuse to screen."
        )
    return got


def screening_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    return SCREEN_START, SCREEN_END


def assert_scores_are_screening_only(start, end) -> None:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start < SCREEN_START or end > SCREEN_END:
        raise RuntimeError(
            f"Refuse to use {start.date()}–{end.date()} for v3 selection; "
            "screening is 2023-01-01–2024-12-31 only (2025–26 and v1 holdouts are out)."
        )


def screening_pass(hid: str, n_closed: int, sharpe: float, maxdd: float) -> tuple[bool, str]:
    """Return (passed, first_failed_gate)."""
    floor = MIN_TRADES[hid]
    if n_closed < floor:
        return False, f"n={n_closed} < {floor}"
    if not (sharpe > 0):
        return False, f"Sharpe={sharpe:.4f} not > 0"
    if not (maxdd > MAX_DD_FLOOR):  # better than -15% means greater than -0.15 (e.g. -0.10)
        return False, f"maxDD={maxdd:.4%} not shallower than -15%"
    return True, ""


def allocate_slots(rows: list[dict]) -> list[dict]:
    """rows must already be screening-window 2x metrics with pass flags. Max 3. No fillers."""
    passed = [r for r in rows if r.get("screen_pass")]
    # At most one event-filter book (only H5 is in that family).
    # Rank by Sharpe desc, then shallower maxDD (maxDD closer to 0, i.e. greater).
    passed.sort(key=lambda r: (-float(r["Sharpe"]), -float(r["maxDD"])))
    return passed[:MAX_SLOTS]


def write_freeze(payload: dict, path: Path = LOCK_PATH) -> Path:
    if path.exists():
        raise AlreadyFrozen(f"freeze already exists at {path}; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "frozen_at": ts_saigon(),
        "spec": "strategy-v3-brief",
        "sha256_brief": assert_brief_hash(),
        "brief_path": str(BRIEF_PATH),
        **payload,
        "note": (
            "DATA ALREADY SEEN screening only; not a PASS. No forward paper runner started. "
            "v2 official runner is untouched. Paper only; no keys; 6,788 USDT excluded. "
            "C_paper is the required sizing base."
        ),
    }
    path.write_text(json.dumps(body, indent=2, default=str))
    return path
