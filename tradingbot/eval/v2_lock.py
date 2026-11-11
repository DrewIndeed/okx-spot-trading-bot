"""Write the spec v2 freeze file (hashes of spec, V2-CORE code/params, V-C code/params)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.eval.protocol import sha256_files, sha256_params
from tradingbot.strategies import spec_v2_common as P

SAIGON = ZoneInfo("Asia/Saigon")
ARTIFACT = Path("/workspace/sprint/artifacts/spec_v2")
FREEZE_PATH = ARTIFACT / "freeze.json"
LOCK_PATH = ARTIFACT / "lock.json"

V2_CORE_PATHS = [
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v2_common.py"),
    Path("/workspace/trading-bot/tradingbot/strategies/spec_v2_core.py"),
    Path("/workspace/trading-bot/tradingbot/eval/v2_engine.py"),
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_paths(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        h.update(str(p).encode())
        h.update(p.read_bytes() if p.exists() else b"MISSING")
    return h.hexdigest()


def sha256_params_v2() -> str:
    blob = json.dumps(P.SPEC_V2_CORE_PARAMS, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def ts_saigon(now: datetime | None = None) -> str:
    now = now or datetime.now(SAIGON)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SAIGON)
    return now.strftime("%Y-%m-%d %H:%M:%S %z") + " (Asia/Saigon, UTC+7)"


def build_freeze() -> dict:
    spec_path = Path(P.SPEC_V2_PATH)
    return {
        "frozen_at": ts_saigon(),
        "spec": "strategy-spec v2",
        "t0_close": "2026-11-12 07:00 UTC+7 (1Dutc close, 2026-11-12 00:00 UTC)",
        "official_run_id": P.OFFICIAL_RUN_ID,
        "sha256_spec_v2": sha256_file(spec_path),
        "spec_v2_path": str(spec_path),
        "sha256_v2_core_code": sha256_paths(V2_CORE_PATHS),
        "v2_core_paths": [str(p) for p in V2_CORE_PATHS],
        "sha256_v2_core_params": sha256_params_v2(),
        "v2_core_params": P.SPEC_V2_CORE_PARAMS,
        "sha256_vc_code": sha256_files(),  # exact v1 protocol set; must stay d893b9a3…
        "sha256_vc_params": sha256_params(),  # must stay 4df0cece…
        "note": (
            "V2-ZARA-FROZEN is V-C unchanged. Official paper-forward first counted bar "
            "is the T0 close. Bars before T0 are not counted. Paper only; no keys; no live. "
            "6,788 USDT wedding funds excluded. C_paper is the required sizing base."
        ),
        "survivorship_zara": "V-C universe remains the current favorites.txt list.",
    }


def write_freeze(path: Path = FREEZE_PATH) -> Path:
    if path.exists():
        raise FileExistsError(
            f"freeze already exists at {path}; refusing to overwrite a frozen spec"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_freeze()
    text = json.dumps(payload, indent=2, default=str)
    path.write_text(text)
    LOCK_PATH.write_text(text)
    return path
