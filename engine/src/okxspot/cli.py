from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from okxspot import DISCLAIMER, __version__
from okxspot.config import load_settings
from okxspot.errors import LiveGuardError, OkxSpotError
from okxspot.logging_config import configure_logging
from okxspot.demo import demo_keys_from_env, missing_demo_keys_message
from okxspot.alerts import build_alerter
from okxspot.killswitch import KillSwitch
from okxspot.watchdog import PROTECTED_PID, Supervisor, monitor_heartbeat
from okxspot.runtime import (
    prepare_live,
    run_backtest,
    run_backtest_history,
    run_demo,
    run_live_loop,
    run_paper_loop,
)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="okxspot",
        description=DISCLAIMER,
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backtest = sub.add_parser("backtest", help="event-driven backtest (demo data unless wired)")
    backtest.add_argument("--config", required=True)
    backtest.add_argument("--output", default="artifacts/backtest")
    backtest.add_argument("--demo", action="store_true", help="use built-in synthetic candles")
    backtest.add_argument("--start", help="UTC start date YYYY-MM-DD for OKX history")
    backtest.add_argument("--end", help="UTC end date YYYY-MM-DD for OKX history")

    paper = sub.add_parser("paper", help="paper trading (default)")
    paper.add_argument("--config", required=True)
    paper.add_argument("--once", action="store_true")
    paper.add_argument("--demo", action="store_true", help="offline synthetic candles, no network")

    live = sub.add_parser("live", help="real spot orders; refused unless every gate passes")
    live.add_argument("--config", required=True)
    live.add_argument("--enable-live", action="store_true")
    live.add_argument("--once", action="store_true")


    kill = sub.add_parser("kill", help="trip the kill switch and block new orders")
    kill.add_argument("--config", required=True)
    kill.add_argument("--reason", default="manual")
    kill.add_argument("--cancel-open", action="store_true")

    rearm = sub.add_parser("rearm", help="manually re-arm a tripped kill switch")
    rearm.add_argument("--config", required=True)

    demo = sub.add_parser(
        "demo-dry-run",
        help="OKX demo trading dry-run; exits cleanly if no demo key is present",
    )
    demo.add_argument("--config", required=True)

    sup = sub.add_parser("supervise", help="restart a paper/demo command; never touches pid 129703")
    sup.add_argument("--config", required=True)
    sup.add_argument("--protect-pid", type=int, default=PROTECTED_PID)
    sup.add_argument("command", nargs=argparse.REMAINDER)

    mon = sub.add_parser("monitor", help="fail closed if the supervisor heartbeat is stale")
    mon.add_argument("--config", required=True)
    mon.add_argument("--max-age", type=float, default=90.0)

    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    environ = env if env is not None else os.environ
    try:
        if args.cmd == "backtest":
            settings = load_settings(args.config)
            output = Path(args.output)
            if args.demo:
                result = run_backtest(settings, output=output)
            else:
                if not args.start or not args.end:
                    raise OkxSpotError("backtest requires --demo or both --start and --end")
                result = run_backtest_history(
                    settings,
                    start=_parse_day(args.start),
                    end=_parse_day(args.end),
                    output=output,
                )
            _print_summary(result.report)
            print(f"wrote {args.output}/backtest.json and equity.png")
            return 0
        if args.cmd == "paper":
            settings = load_settings(args.config)
            if args.demo:
                result = run_demo(settings)
                _print_summary(result.report)
                print("paper demo complete. This is not a performance claim.")
                return 0
            run_paper_loop(settings, once=args.once, demo=False)
            print(f"paper cycle finished. {DISCLAIMER}")
            return 0
        if args.cmd == "live":
            settings = load_settings(args.config)
            client = prepare_live(
                settings,
                enable_live_flag=args.enable_live,
                env=environ,
            )
            run_live_loop(settings, client, once=args.once)
            return 0
        if args.cmd == "kill":
            settings = load_settings(args.config)
            ks = KillSwitch(settings.kill_switch_path)
            ks.trip(args.reason, cancel_open=args.cancel_open, source="cli")
            print(f"kill switch tripped reason={args.reason} path={settings.kill_switch_path}")
            return 0
        if args.cmd == "rearm":
            settings = load_settings(args.config)
            ks = KillSwitch(settings.kill_switch_path)
            ks.rearm(operator="cli")
            print(f"kill switch rearmed path={settings.kill_switch_path}")
            return 0
        if args.cmd == "demo-dry-run":
            load_settings(args.config)
            keys = demo_keys_from_env(environ)
            if keys is None:
                print(missing_demo_keys_message())
                return 0
            print(
                "Demo key is present (redacted). No order was sent. "
                "A mocked or operator-supervised demo session is required next; "
                "this dry-run only confirmed that demo credentials exist."
            )
            return 0
        if args.cmd == "supervise":
            settings = load_settings(args.config)
            command = list(args.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                raise OkxSpotError("supervise requires a command after --")
            supervisor = Supervisor(
                command,
                heartbeat_path=settings.heartbeat_path,
                kill_switch=KillSwitch(settings.kill_switch_path),
                alerter=build_alerter(settings),
                protected_pids=frozenset({int(args.protect_pid), PROTECTED_PID}),
                child_heartbeat_path=settings.child_heartbeat_path,
            )
            supervisor.run_until_stopped()
            return 0
        if args.cmd == "monitor":
            settings = load_settings(args.config)
            tripped = monitor_heartbeat(
                heartbeat_path=settings.heartbeat_path,
                kill_switch=KillSwitch(settings.kill_switch_path),
                max_age_seconds=args.max_age,
                protected_pids=frozenset({PROTECTED_PID}),
            )
            print("monitor: stale heartbeat; kill switch tripped" if tripped else "monitor: heartbeat ok")
            return 1 if tripped else 0
    except LiveGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OkxSpotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"okxspot {__version__}: unknown command", file=sys.stderr)
    return 1


def _parse_day(value: str) -> datetime:
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise OkxSpotError(f"date must be YYYY-MM-DD, got {value}") from exc
    return day.replace(tzinfo=UTC)


def _print_summary(report: dict[str, object]) -> None:
    full = report.get("full")
    trades = full.get("n_trades") if isinstance(full, dict) else None
    end = full.get("end_equity") if isinstance(full, dict) else None
    print(report.get("disclaimer"))
    print(f"trades={trades} end_equity={end} strategy={report.get('strategy')}")
