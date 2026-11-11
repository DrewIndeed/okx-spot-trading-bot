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
