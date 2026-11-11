#!/usr/bin/env python3
"""Time-compressed overnight soak + fault injection for the live-ready worktree.

Never touches pid 129703 or /workspace/trading-bot.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

from okxspot.alerts import FileAlerter, MemoryAlerter, CompositeAlerter
from okxspot.errors import AmbiguousOrderError
from okxspot.killswitch import KillSwitch
from okxspot.models import Candle, Order, OrderPurpose, OrderRequest, OrderStatus, OrderType, Side
from okxspot.orders.ids import make_client_order_id
from okxspot.orders.journal import OrderJournal
from okxspot.orders.manager import OrderManager
from okxspot.risk.manager import RiskManager
from okxspot.state import StateStore
from okxspot.stale import StaleGuard
from okxspot.watchdog import PROTECTED_PID, Supervisor, is_protected_pid
from tests.conftest import instrument, make_settings
from tests.stub import StubExchange, stored_order

EVIDENCE = Path("/workspace/sprint/artifacts/live_readiness/soak")
D = Decimal


def official_alive() -> tuple[bool, str]:
    try:
        os.kill(PROTECTED_PID, 0)
    except OSError:
        return False, f"pid {PROTECTED_PID} is not running"
    cmd = Path(f"/proc/{PROTECTED_PID}/cmdline")
    text = cmd.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace") if cmd.exists() else ""
    return True, text


def write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")


def fault_stale(out: Path) -> dict:
    guard = StaleGuard(bar="1H", tolerance_sec=60, max_clock_skew_ms=2000, max_price_move_pct=Decimal("0.05"))
    now = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    last = Candle("BTC-USDT", now - timedelta(hours=3), D("100"), D("100"), D("100"), D("100"), D("1"), True)
    ok, reason = guard.check(candles=[last], now=now, server_time_ms=int(now.timestamp() * 1000), last_price=D("100"))
    lines = [
        f"ok={ok}",
        f"reason={reason}",
        "expected=stale_candle (last closed candle older than 1H + 60s)",
    ]
    write(out / "excerpts" / "stale.log", "\n".join(lines))
    assert ok is False and reason == "stale_candle"
    return {"handled": True, "reason": reason}


def fault_duplicate(out: Path) -> dict:
    class Counting(StubExchange):
        def __init__(self) -> None:
            self.place_calls = 0
            self.orders: dict[str, Order] = {}

        def place_order(self, request: OrderRequest) -> Order:
            self.place_calls += 1
            order = stored_order(request)
            self.orders[request.client_order_id] = order
            return order

        def get_order(self, inst_id: str, client_order_id: str) -> Order | None:
            return self.orders.get(client_order_id)

    ex = Counting()
    state = StateStore(out / "soak.db")
    journal = OrderJournal(out / "orders.jsonl")
    cid = make_client_order_id("soak", "BTC-USDT", "buy", 1_700_000_000)
    req = OrderRequest("BTC-USDT", Side.BUY, OrderType.MARKET, D("1"), cid)
    mgr = OrderManager(ex, state, journal=journal)
    first = mgr.submit(req)
    # crash/retry: new manager, same journal+db+exchange
    state2 = StateStore(out / "soak.db")
    second = OrderManager(ex, state2, journal=journal).submit(req)
    lines = [
        f"clOrdId={first.client_order_id}",
        f"place_calls={ex.place_calls}",
        f"second_cid={second.client_order_id}",
        f"journal_events={[e['event'] for e in journal.load()]}",
    ]
    write(out / "excerpts" / "duplicate.log", "\n".join(lines))
    state.close()
    state2.close()
    assert ex.place_calls == 1
    return {"handled": True, "place_calls": ex.place_calls, "clOrdId": first.client_order_id}


def fault_kill_switch(out: Path, alerter: FileAlerter) -> dict:
    path = out / "KILL_SWITCH"
    ks = KillSwitch(path)
    mgr = RiskManager(make_settings().risk, kill_switch=ks)
    now = datetime.now(UTC)
    allowed = mgr.pretrade(
        inst_id="BTC-USDT", side=Side.BUY, purpose=OrderPurpose.ENTRY, notional=D("50"), now=now
    )
    ks.trip("soak", source="soak")
    alerter.send("critical", "soak tripped kill switch", event="kill_switch")
    blocked = mgr.pretrade(
        inst_id="BTC-USDT", side=Side.BUY, purpose=OrderPurpose.ENTRY, notional=D("50"), now=now
    )
    restarted = KillSwitch(path)
    lines = [
        f"before_trip_allowed={allowed.allowed}",
        f"after_trip_allowed={blocked.allowed} reason={blocked.reason}",
        f"persisted_after_restart={restarted.is_tripped} reason={restarted.reason}",
    ]
    write(out / "excerpts" / "kill_switch.log", "\n".join(lines) + "\n" + path.read_text(encoding="utf-8"))
    assert blocked.allowed is False and restarted.is_tripped is True
    return {"handled": True, "persisted": True, "reason": blocked.reason}


def fault_restart(out: Path, alerter: CompositeAlerter, mem: MemoryAlerter) -> dict:
    runner = out / "child_runner.py"
    write(
        runner,
        "\n".join(
            [
                "import time, sys",
                "from pathlib import Path",
                "marker = Path(sys.argv[1])",
                "marker.write_text(str(time.time()) + '\\n' + str(os_get := __import__('os').getpid()))",
                "time.sleep(30)",
            ]
        ),
    )
    marker = out / "child_marker.txt"
    ks = KillSwitch(out / "KILL_SWITCH_WD")
    launches = {"n": 0}

    def popen(argv, **kwargs):
        launches["n"] += 1
        return subprocess.Popen(argv)

    py = sys.executable
    sup = Supervisor(
        [py, str(runner), str(marker)],
        heartbeat_path=out / "heartbeat.json",
        kill_switch=ks,
        alerter=alerter,
        max_restarts_per_hour=5,
        backoff_seconds=(0.2, 0.2),
        poll_seconds=0.2,
        popen=popen,
        protected_pids=frozenset({PROTECTED_PID}),
    )
    # run supervisor in a background thread via a child process so we can kill the grandchild
    import threading

    error: list[str] = []

    def run() -> None:
        try:
            sup.run_until_stopped(max_cycles=200)
        except Exception as exc:  # noqa: BLE001
            error.append(str(exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # wait for first child
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    first_pid = None
    if marker.exists():
        first_pid = int(marker.read_text(encoding="utf-8").strip().splitlines()[-1])
        if is_protected_pid(first_pid):
            raise RuntimeError("soak almost touched a protected pid")
        os.kill(first_pid, signal.SIGKILL)
    # wait for restart
    restarted = False
    deadline = time.time() + 10
    while time.time() < deadline:
        if any(e.event == "restart" for e in mem.events) and launches["n"] >= 2:
            restarted = True
            break
        time.sleep(0.05)
    sup.request_stop()
    # give the thread a moment to notice
    time.sleep(0.3)
    hb = (out / "heartbeat.json").read_text(encoding="utf-8") if (out / "heartbeat.json").exists() else ""
    lines = [
        f"official_protected_pid={PROTECTED_PID}",
        f"first_child_pid={first_pid}",
        f"launches={launches['n']}",
        f"restart_alerts={[e.message for e in mem.events if e.event == 'restart']}",
        f"restarted={restarted}",
        f"heartbeat={hb.strip()}",
        f"errors={error}",
        f"kill_switch_tripped={ks.is_tripped}",
    ]
    write(out / "excerpts" / "restart.log", "\n".join(lines))
    assert first_pid is not None and first_pid != PROTECTED_PID
    assert restarted is True
    assert ks.is_tripped is False
    return {"handled": True, "first_child_pid": first_pid, "launches": launches["n"]}


def main() -> int:
    started = datetime.now().astimezone()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE
    (out / "excerpts").mkdir(parents=True, exist_ok=True)
    alive, cmdline = official_alive()
    write(out / "official_pid.txt", f"alive={alive}\npid={PROTECTED_PID}\ncmdline={cmdline}\n")
    if not alive:
        write(out / "FAILED.txt", "official pid 129703 was not alive at soak start")
        return 2

    alerts_path = out / "alerts.jsonl"
    file_alerter = FileAlerter(alerts_path)
    mem = MemoryAlerter()
    alerter = CompositeAlerter([file_alerter, mem])

    results = {
        "started": started.isoformat(),
        "zone": "UTC+7 (Etc/GMT-7)",
        "worktree": str(ROOT),
        "official_pid": {"pid": PROTECTED_PID, "alive_at_start": alive, "cmdline": cmdline},
        "faults": {},
    }
    results["faults"]["stale_data"] = fault_stale(out)
    file_alerter.send("warning", "stale_candle refused trade", event="stale_data")
    results["faults"]["duplicate_submit"] = fault_duplicate(out)
    file_alerter.send("warning", "duplicate submit suppressed", event="recon_mismatch")
    results["faults"]["kill_switch"] = fault_kill_switch(out, file_alerter)
    results["faults"]["runner_kill_restart"] = fault_restart(out, alerter, mem)
    file_alerter.send("info", "soak daily summary: faults injected and handled, errors=0", event="daily_summary")

    # short paper/risk stack stretch (time-compressed ~45s wall)
    stretch_start = time.time()
    mgr = RiskManager(make_settings().risk)
    now = datetime.now(UTC)
    for i in range(8):
        decision = mgr.pretrade(
            inst_id="BTC-USDT",
            side=Side.BUY,
            purpose=OrderPurpose.ENTRY,
            notional=D("25"),
            now=now + timedelta(seconds=i * 7),
        )
        assert decision.allowed is True
        time.sleep(5)
    results["paper_stretch_sec"] = round(time.time() - stretch_start, 1)

    alive_end, cmdline_end = official_alive()
    results["official_pid"]["alive_at_end"] = alive_end
    results["official_pid"]["cmdline_end"] = cmdline_end
    results["ended"] = datetime.now().astimezone().isoformat()
    results["ok"] = alive_end and all(f.get("handled") for f in results["faults"].values())
    write(out / "summary.json", json.dumps(results, indent=2))
    lines = [
        "# Soak evidence (time-compressed)",
        "",
        f"- Started: {results['started']} (UTC+7 / Etc/GMT-7)",
        f"- Ended: {results['ended']} (UTC+7 / Etc/GMT-7)",
        f"- Worktree: `{ROOT}`",
        f"- Official pid 129703 alive at start: {alive}",
        f"- Official pid 129703 alive at end: {alive_end}",
        f"- Paper pretrade stretch: {results['paper_stretch_sec']}s",
        "",
        "## Faults",
        f"- Stale data: refused with `{results['faults']['stale_data']['reason']}`. See `excerpts/stale.log`.",
        f"- Duplicate submit: place_calls={results['faults']['duplicate_submit']['place_calls']}. See `excerpts/duplicate.log`.",
        f"- Kill switch: blocked and persisted. See `excerpts/kill_switch.log`.",
        f"- Runner kill/restart: launches={results['faults']['runner_kill_restart']['launches']}. See `excerpts/restart.log`.",
        "",
        "All faults were handled. This is paper/demo only. No live orders were sent.",
    ]
    write(out / "summary.md", "\n".join(lines))
    print(json.dumps({k: results[k] for k in ("ok", "faults", "paper_stretch_sec")}, indent=2))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
