"""spec_v4_shadow: forward paper runner for the frozen V4-COMBO shadow slot (paper only).

Cypheous 12/11: v4 NOT PASS; V4-COMBO accepted only as ONE extra forward paper SHADOW slot.
It does not replace v3 or v2. No keys, no orders, no live. C_paper=10000 only (never the
6,788 USDT wedding funds, never an account balance). Costs 2x = 40 bps/side.

Each poll is a pure, deterministic replay of the frozen backtest path (run_spec_v4.run_book)
over closed 1Dutc bars from the first fill after T0 to the last closed bar, so restarts are
idempotent and nothing before T0 is counted. Refuses to run on any freeze hash mismatch.
"""
from __future__ import annotations

import argparse, hashlib, json, os, signal, subprocess, sys, time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path("/workspace/trading-bot-v4")
ART = Path("/workspace/sprint/artifacts/spec_v4")
FREEZE_PATH = ART / "freeze_v4.json"
RUN_ID = "spec_v4_shadow"
CANDIDATE = "V4-COMBO"
C_PAPER = 10000.0
WEDDING = 6788.0
COST = 0.004  # 2x = 40 bps/side
T0_BAR = pd.Timestamp("2026-11-12")  # 1Dutc bar opened 12/11 00:00 UTC; closes 13/11 07:00 UTC+7
T0_LABEL = "2026-11-13 07:00 UTC+7 (first 1Dutc close after the freeze)"
SAIGON = ZoneInfo("Asia/Saigon")
_E = ROOT / "tradingbot"
V4_CODE_PATHS = [
    _E / "eval/v4_exits.py", _E / "eval/run_spec_v4.py", _E / "eval/v3_hypotheses.py",
    _E / "eval/v3_engine.py", _E / "eval/v2_engine.py", _E / "eval/v3_load.py",
    _E / "eval/regime.py", _E / "strategies/spec_v2_common.py", _E / "universe.py",
]
BRIEF_PATHS = [ART / "summary.md", ART / "candidate.json", ART / "holdout_results.json"]
NOTES = (
    "No more v4 tuning rounds. Shadow slot only: does not replace v3 or v2. "
    "Replacement criteria, evaluated only at T+180 forward (T+90 is interim only): beat H5 AND "
    "V2-CORE on Sharpe; maxDD better than -15% AND <= 50% of BENCH-50-50 DD; net >= 0 at 2x "
    "(40 bps/side); n >= 30 closed trades; DSR(N=16) >= 0.95 (also report N=51)."
)


class FreezeMismatch(RuntimeError):
    """Frozen v4 code or params do not match freeze_v4.json. Refuse to start."""


def ts_saigon() -> str:
    return datetime.now(SAIGON).strftime("%Y-%m-%d %H:%M:%S %z") + " (Asia/Saigon, UTC+7)"


def _sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def code_hash(paths=None) -> str:
    h = hashlib.sha256()
    for p in sorted(paths or V4_CODE_PATHS, key=str):
        p = Path(p)
        h.update(str(p).encode())
        h.update(p.read_bytes() if p.exists() else b"MISSING")
    return h.hexdigest()


def frozen_params() -> dict:
    from tradingbot.eval.run_spec_v4 import V4
    from tradingbot.eval.v3_hypotheses import params_for

    return {
        "name": CANDIDATE, "base": "H5", "base_params": params_for("H5"),
        "overlay": asdict(V4[CANDIDATE]), "cost_per_side": COST, "c_paper": C_PAPER,
        "fill": "signal on closed 1Dutc t; exits fill before entries at open of t+1",
        "spot_only": "long-only; no shorting, margin or derivatives",
    }


def params_hash() -> str:
    return hashlib.sha256(json.dumps(frozen_params(), sort_keys=True, default=str).encode()).hexdigest()


def brief_hash() -> str:
    h = hashlib.sha256()
    for p in BRIEF_PATHS:
        h.update(p.name.encode()); h.update(Path(p).read_bytes())
    return h.hexdigest()


def write_freeze(path: Path = FREEZE_PATH, *, frozen_at: str | None = None) -> Path:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} is write-once")
    git = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    f = {
        "spec": "spec_v4", "candidate": CANDIDATE, "run_id": RUN_ID, "status": "FROZEN shadow slot (NOT PASS)",
        "frozen_at": frozen_at or ts_saigon(), "git_head_at_freeze": git,
        "t0": T0_LABEL, "t0_bar_open_utc": str(T0_BAR.date()),
        "sha256_v4_code": code_hash(), "v4_code_paths": [str(p) for p in V4_CODE_PATHS],
        "v4_code_file_sha256": {str(p): _sha_file(p) for p in V4_CODE_PATHS},
        "sha256_params": params_hash(), "params": frozen_params(),
        "sha256_brief": brief_hash(), "brief_paths": [str(p) for p in BRIEF_PATHS],
        "brief_file_sha256": {str(p): _sha_file(p) for p in BRIEF_PATHS},
        "books": [CANDIDATE, "BENCH-50-50"], "c_paper": C_PAPER, "cost": "2x = 40 bps/side",
        "refuse_on_mismatch": True, "N_official": 16, "N_conservative": 51, "notes": NOTES,
    }
    path.write_text(json.dumps(f, indent=2, default=str) + "\n")
    return path


def verify_freeze(path: Path = FREEZE_PATH) -> dict:
    f = json.loads(Path(path).read_text())
    got = code_hash()
    if got != f["sha256_v4_code"]:
        raise FreezeMismatch(f"v4 code hash mismatch: got {got} expected {f['sha256_v4_code']}")
    gp = params_hash()
    if gp != f["sha256_params"]:
        raise FreezeMismatch(f"v4 params hash mismatch: got {gp} expected {f['sha256_params']}")
    return {"ok": True, "sha256_v4_code": got, "sha256_params": gp, "sha256_brief": f["sha256_brief"],
            "frozen_at": f["frozen_at"], "freeze_path": str(path)}


def counted_window(last_closed: pd.Timestamp):
    """Counted bars: first fill bar (T0+1) .. last closed bar. None until the fill bar has closed."""
    start = T0_BAR + pd.Timedelta(days=1)
    if last_closed is None or pd.Timestamp(last_closed) < start:
        return None
    return start, pd.Timestamp(last_closed)


def load_ctx():
    from tradingbot.eval.run_spec_v4 import setup

    return setup()


def refresh_cache() -> None:
    from tradingbot.cli import _feed
    from tradingbot.config import load_config
    from tradingbot.universe import inst_id, load_favorites

    feed = _feed(load_config(str(ROOT / "config.yaml")))
    feed.cache_dir = ROOT / "data" / "cache"
    for base in ["BTC", "ETH"] + [b for b in load_favorites() if b not in ("BTC", "ETH")]:
        feed.get_candles(inst_id(base), "1Dutc", days=400, update=True, closed_only=True)


def forward_books(ctx, last_closed) -> dict:
    from tradingbot.eval.run_spec_v4 import run_book

    w = counted_window(last_closed)
    if w is None:
        return {}
    out = {}
    for name in (CANDIDATE, "BENCH-50-50"):
        eq, rets, trades, expo, turn = run_book(name, ctx, w[0], w[1], COST)
        out[name] = {"equity": eq, "rets": rets, "trades": trades, "exposure": expo}
    return out


def run_dirs(root: Path = ROOT) -> dict:
    st, lg, art = root / "state/spec_v4" / RUN_ID, root / "logs/spec_v4" / RUN_ID, ART / RUN_ID
    for p in (st, lg, art):
        p.mkdir(parents=True, exist_ok=True)
    return {"state": st, "logs": lg, "art": art, "pidfile": st / "paper.pid", "log": lg / "paper_runner.log",
            "status": art / "paper_status.json", "lock": root / "RUNNING.lock"}


def iteration(*, update: bool = True) -> dict:
    errors: list[str] = []
    info = verify_freeze()
    if update:
        try:
            refresh_cache()
        except Exception as e:  # keep running on stale data, report it
            errors.append(f"cache refresh failed: {e}")
    ctx = load_ctx()
    last = ctx[0]["close"].index[-1]
    books = {}
    try:
        for name, b in forward_books(ctx, last).items():
            eq = b["equity"]
            dd = float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0
            books[name] = {"equity": float(eq.iloc[-1]), "first_counted_bar": str(pd.Timestamp(eq.index[0]).date()),
                           "n_counted_bars": int(len(eq)), "maxDD": dd, "n_trades": len(b["trades"]),
                           "exposure": float(b["exposure"].iloc[-1]) if len(b["exposure"]) else 0.0}
    except Exception as e:
        errors.append(f"forward replay failed: {e}")
    waiting = counted_window(last) is None
    payload = {
        "run_id": RUN_ID, "candidate": CANDIDATE, "shadow": True, "updated": ts_saigon(),
        "t0": T0_LABEL, "last_closed_bar_open_utc": str(pd.Timestamp(last).date()),
        "waiting_for_t0": waiting, "t0_closed": pd.Timestamp(last) >= T0_BAR,
        "books": books if books else {n: {"equity": C_PAPER, "n_counted_bars": 0} for n in (CANDIDATE, "BENCH-50-50")},
        "c_paper": C_PAPER, "cost": "2x 40 bps/side", "freeze": info, "errors": errors, "n_errors": len(errors),
        "note": "Paper shadow only. No keys, no orders. Pre-T0 bars are never counted. " + NOTES,
    }
    d = run_dirs()
    tmp = d["status"].with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str)); os.replace(tmp, d["status"])
    return payload


def lock_text(pid: int) -> str:
    return "\n".join([
        "owner: Tobias - Quant Trading Engineer",
        f"pid: {pid}",
        f"process: .venv/bin/python -m tradingbot.eval.v4_paper start --poll 60  (loop: -m tradingbot.eval.v4_paper loop --poll 60)",
        f"run_id: {RUN_ID}",
        "purpose: spec v4 SHADOW paper-forward slot V4-COMBO + BENCH 50/50 reference (paper only, no keys, no live). Cypheous: NOT PASS; shadow only, does not replace v3 or v2.",
        f"started: {ts_saigon()}",
        f"t0: {T0_LABEL}",
        "c_paper: 10000 (never the 6,788 USDT wedding funds; never an OKX account balance)",
        f"freeze: {FREEZE_PATH}",
        "rule: do NOT copy, re-clone, git clean/reset, or delete .venv/state/logs/data in this folder while this file exists. Do not touch v2 (/workspace/trading-bot) or v3 (/workspace/trading-bot-v3).",
        "",
    ])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def start(*, poll: int = 60, freeze_path: Path = FREEZE_PATH, root: Path = ROOT) -> int:
    verify_freeze(freeze_path)  # refuse before spawning anything
    d = run_dirs(root)
    if d["pidfile"].exists() and _alive(int(d["pidfile"].read_text().strip() or 0)):
        raise RuntimeError(f"already running pid={d['pidfile'].read_text().strip()}")
    log = open(d["log"], "a")
    proc = subprocess.Popen([sys.executable, "-m", "tradingbot.eval.v4_paper", "loop", "--poll", str(poll)],
                            cwd=str(root), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    d["pidfile"].write_text(str(proc.pid))
    d["lock"].write_text(lock_text(proc.pid))
    return proc.pid


def stop(root: Path = ROOT) -> str:
    d = run_dirs(root)
    if not d["pidfile"].exists():
        return "not running"
    pid = int(d["pidfile"].read_text().strip())
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(80):
            if not _alive(pid):
                break
            time.sleep(0.1)
    d["pidfile"].unlink(missing_ok=True)
    if d["lock"].exists() and f"pid: {pid}" in d["lock"].read_text():
        d["lock"].unlink()
    return f"stopped pid={pid}"


def loop(poll: int = 60) -> int:
    d = run_dirs()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            p = iteration()
            print(json.dumps({"ts": p["updated"], "waiting_for_t0": p["waiting_for_t0"], "last_closed": p["last_closed_bar_open_utc"],
                              "n_errors": p["n_errors"], "errors": p["errors"]}), flush=True)
        except FreezeMismatch as e:
            print(json.dumps({"ts": ts_saigon(), "fatal": f"freeze mismatch, refusing: {e}"}), flush=True)
            d["pidfile"].unlink(missing_ok=True)
            return 2
        except Exception as e:
            print(json.dumps({"ts": ts_saigon(), "error": str(e)}), flush=True)
        time.sleep(poll)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tradingbot.eval.v4_paper")
    ap.add_argument("cmd", choices=["freeze", "verify", "start", "stop", "status", "loop", "once"])
    ap.add_argument("--poll", type=int, default=60)
    a = ap.parse_args(argv)
    if a.cmd == "freeze":
        print(write_freeze()); return 0
    if a.cmd == "verify":
        print(json.dumps(verify_freeze(), indent=1)); return 0
    if a.cmd == "start":
        print(f"started pid={start(poll=a.poll)} run_id={RUN_ID}"); return 0
    if a.cmd == "stop":
        print(stop()); return 0
    if a.cmd == "status":
        print(run_dirs()["status"].read_text()); return 0
    if a.cmd == "once":
        print(json.dumps(iteration(update=False), indent=1, default=str)); return 0
    return loop(a.poll)


if __name__ == "__main__":
    raise SystemExit(main())
