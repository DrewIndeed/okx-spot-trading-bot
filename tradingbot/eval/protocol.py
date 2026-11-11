"""Frozen walk-forward / holdout protocol, lock file, and §8 acceptance."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.strategies import spec_v1_common as P

SAIGON = ZoneInfo("Asia/Saigon")
ARTIFACT_DIR = Path("/workspace/sprint/artifacts/spec_v1")
LOCK_PATH = ARTIFACT_DIR / "lock.json"
LOCK_PATH_PARENT = Path("/workspace/sprint/artifacts/spec_v1_lock.json")
WF_METRICS = ARTIFACT_DIR / "wf_metrics.json"
HOLDOUT = {"H1": ARTIFACT_DIR / "holdout_H1.json", "H2": ARTIFACT_DIR / "holdout_H2.json"}

CODE_PATHS = [
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v1_common.py"),
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v1_signals.py"),
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v1_va.py"),
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v1_vb.py"),
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v1_vc.py"),
    Path("/workspace/trading-bot/tradingbot/eval/load.py"),
    Path("/workspace/trading-bot/tradingbot/eval/events.py"),
    Path("/workspace/trading-bot/tradingbot/eval/regime.py"),
    Path("/workspace/trading-bot/tradingbot/eval/metrics.py"),
    Path("/workspace/trading-bot/tradingbot/eval/engine.py"),
    Path("/workspace/trading-bot/tradingbot/eval/protocol.py"),
    Path("/workspace/trading-bot/tradingbot/eval/run_spec_v1.py"),
    Path("/workspace/trading-bot/tradingbot/eval/calendars/events_f2_v1.csv"),
    Path("/workspace/trading-bot/tradingbot/eval/calendars/FETCH_STAMP.txt"),
]


def ts_saigon(now: datetime | None = None) -> str:
    now = now or datetime.now(SAIGON)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SAIGON)
    return now.strftime("%Y-%m-%d %H:%M:%S %z") + " (Asia/Saigon, UTC+7)"


def windows(last_closed: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    def p(a, b):
        return pd.Timestamp(a), pd.Timestamp(b)

    out = {
        "F1": p(*P.FOLDS[0][1:]),
        "F2": p(*P.FOLDS[1][1:]),
        "F3": p(*P.FOLDS[2][1:]),
        "F4": p(*P.FOLDS[3][1:]),
        "POOLED_OOS": p(*P.POOLED_OOS),
        "BUFFER": p(*P.BUFFER),
        "H1": p(*P.HOLDOUT_H1),
        "H2": (pd.Timestamp(P.HOLDOUT_H2_START), pd.Timestamp(last_closed)),
    }
    return out


def sha256_files(paths=None) -> str:
    h = hashlib.sha256()
    for p in sorted(paths or CODE_PATHS, key=lambda x: str(x)):
        p = Path(p)
        h.update(str(p).encode())
        if p.exists():
            h.update(p.read_bytes())
        else:
            h.update(b"MISSING")
    return h.hexdigest()


def sha256_params() -> str:
    blob = json.dumps(P.SPEC_V1_PARAMS, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


class HoldoutAlreadyRun(RuntimeError):
    pass


def assert_holdout_not_run(name: str) -> None:
    path = HOLDOUT[name]
    if path.exists():
        raise HoldoutAlreadyRun(
            f"Refuse to rerun holdout {name}: result already exists at {path}. "
            "The one-shot protocol forbids a second pass."
        )


def write_lock(*, wf_written: bool, extra: dict | None = None) -> Path:
    if not wf_written or not WF_METRICS.exists():
        raise RuntimeError("Refusing to write the holdout lock before WF metrics are on disk.")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "locked_at": ts_saigon(),
        "spec": "strategy-spec v1",
        "sha256_strategy_code": sha256_files(),
        "sha256_params": sha256_params(),
        "params": P.SPEC_V1_PARAMS,
        "wf_metrics": str(WF_METRICS),
        "holdouts_unlocked": True,
        "note": "H1 and H2 may each be run exactly once after this lock. Existing holdout result files are never overwritten.",
        "survivorship": P.SPEC_V1_PARAMS["survivorship"],
    }
    if extra:
        payload["extra"] = extra
    LOCK_PATH.write_text(json.dumps(payload, indent=2, default=str))
    LOCK_PATH_PARENT.write_text(LOCK_PATH.read_text())
    return LOCK_PATH


def _verdict_fold(rows: list[dict]) -> dict:
    """§8.1: Sharpe > 0 in a majority of evaluable folds (≥ 3 of 4)."""
    evaluable = []
    wins = []
    detail = []
    for r in rows:
        ev = r["n_days"] >= P.FOLD_MIN_DAYS and r["n"] >= P.FOLD_MIN_TRADES
        win = ev and r["Sharpe"] > 0
        detail.append({**r, "evaluable": ev, "win": win})
        if ev:
            evaluable.append(r)
            wins.append(win)
    if len(evaluable) < P.FOLD_MAJORITY:
        return {"gate": "8.1_folds", "result": "INSUFFICIENT", "evaluable": len(evaluable), "wins": sum(wins), "detail": detail,
                "reason": f"fewer than {P.FOLD_MAJORITY} folds are evaluable ({len(evaluable)})"}
    ok = sum(wins) >= P.FOLD_MAJORITY
    return {
        "gate": "8.1_folds",
        "result": "PASS" if ok else "FAIL",
        "evaluable": len(evaluable),
        "wins": int(sum(wins)),
        "detail": detail,
        "reason": None if ok else f"Sharpe>0 in {sum(wins)}/{len(evaluable)} evaluable folds (need ≥{P.FOLD_MAJORITY})",
    }


def _verdict_pooled(m2x: dict, m25: dict, m50: dict, btc_sharpe: float, va_sharpe: float | None, variant: str, dsr3: dict) -> dict:
    """§8.2 all bullets. Official n = completed round-trips in the pooled window."""
    reasons = []
    status = "PASS"
    n = m2x["n"]
    if n < P.POOLED_MIN_TRADES:
        return {
            "gate": "8.2_pooled",
            "result": "INSUFFICIENT",
            "reason": f"n trades {n} < {P.POOLED_MIN_TRADES}",
            "n": n,
        }
    if not (m2x["Sharpe"] >= P.POOLED_MIN_SHARPE):
        reasons.append(f"2× Sharpe {m2x['Sharpe']:.3f} < {P.POOLED_MIN_SHARPE}")
        status = "FAIL"
    if not (m2x["maxDD"] > P.MAXDD_FLOOR):
        reasons.append(f"2× maxDD {m2x['maxDD']:.4f} not > {P.MAXDD_FLOOR}")
        status = "FAIL"
    if not (m25["Sharpe"] > 0):
        reasons.append(f"25 bps Sharpe {m25['Sharpe']:.3f} not > 0")
        status = "FAIL"
    if not (m50["Sharpe"] > 0):
        reasons.append(f"50 bps Sharpe {m50['Sharpe']:.3f} not > 0")
        status = "FAIL"
    if not (m2x["Sharpe"] > btc_sharpe):
        reasons.append(f"2× Sharpe {m2x['Sharpe']:.3f} does not beat BTC B&H Sharpe {btc_sharpe:.3f}")
        status = "FAIL"
    if variant in ("V-B", "V-C") and va_sharpe is not None:
        if not (m2x["Sharpe"] > va_sharpe):
            reasons.append(f"2× Sharpe {m2x['Sharpe']:.3f} does not beat V-A Sharpe {va_sharpe:.3f}")
            status = "FAIL"
    dsr_val = dsr3.get("DSR")
    if dsr_val is None or not (dsr_val >= P.DSR_MIN):
        reasons.append(f"DSR(N=3) {dsr_val} < {P.DSR_MIN}")
        status = "FAIL"
    return {
        "gate": "8.2_pooled",
        "result": status,
        "reason": None if status == "PASS" else "; ".join(reasons),
        "n": n,
        "Sharpe_2x": m2x["Sharpe"],
        "maxDD_2x": m2x["maxDD"],
        "Sharpe_25bps": m25["Sharpe"],
        "Sharpe_50bps": m50["Sharpe"],
        "btc_sharpe": btc_sharpe,
        "va_sharpe": va_sharpe,
        "DSR_N3": dsr_val,
    }


def _verdict_holdout(name: str, m2x: dict, killed_before: bool) -> dict:
    if killed_before:
        return {
            "gate": f"8.3_{name}",
            "result": "FAIL",
            "reason": "sticky kill-switch already flattened the curve before this holdout starts",
            "n": m2x.get("n", 0),
        }
    n = m2x["n"]
    if n < P.HOLDOUT_MIN_TRADES:
        return {
            "gate": f"8.3_{name}",
            "result": "INSUFFICIENT",
            "reason": f"n trades {n} < {P.HOLDOUT_MIN_TRADES}",
            "n": n,
        }
    reasons = []
    status = "PASS"
    if not (m2x["Sharpe"] > 0):
        reasons.append(f"Sharpe {m2x['Sharpe']:.3f} not > 0")
        status = "FAIL"
    if not (m2x["maxDD"] > P.MAXDD_FLOOR):
        reasons.append(f"maxDD {m2x['maxDD']:.4f} not > {P.MAXDD_FLOOR}")
        status = "FAIL"
    return {
        "gate": f"8.3_{name}",
        "result": status,
        "reason": None if status == "PASS" else "; ".join(reasons),
        "n": n,
        "Sharpe": m2x["Sharpe"],
        "maxDD": m2x["maxDD"],
    }


def combine_verdicts(parts: list[dict]) -> tuple[str, str | None]:
    """Overall: INSUFFICIENT if any gate is INSUFFICIENT (and none FAIL? or any?).

    Spec: INSUFFICIENT is a valid outcome. Failure on H1 or H2 is FAIL overall.
    If fewer than 3 folds evaluable → INSUFFICIENT. If pooled n<30 → INSUFFICIENT.
    If a holdout n<15 → INSUFFICIENT overall.
    If any gate FAILs, overall is FAIL (a fail beats insufficient for honesty:
    a killed holdout is FAIL even if WF was insufficient). Actually: if folds
    are insufficient AND holdout fails, overall FAIL because 8.3 failed.
    If any FAIL → FAIL. Else if any INSUFFICIENT → INSUFFICIENT. Else PASS.
    """
    results = [p["result"] for p in parts]
    fail = next((p for p in parts if p["result"] == "FAIL"), None)
    if fail:
        return "FAIL", f"first failed gate: {fail['gate']} — {fail.get('reason')}"
    insuf = next((p for p in parts if p["result"] == "INSUFFICIENT"), None)
    if insuf:
        return "INSUFFICIENT", f"first insufficient gate: {insuf['gate']} — {insuf.get('reason')}"
    return "PASS", None
