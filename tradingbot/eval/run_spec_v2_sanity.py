#!/usr/bin/env python3
"""Optional V2-CORE historical sanity check.

DATA ALREADY SEEN — not counted toward PASS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from tradingbot.config import load_config
from tradingbot.eval.load import load_panel
from tradingbot.eval.metrics import max_drawdown, sharpe_daily_series
from tradingbot.eval.regime import build_regime
from tradingbot.eval.v2_engine import curve_series, run_bench_path, run_core_path
from tradingbot.eval.v2_paper import c_paper_from
from tradingbot.strategies.spec_v2_core import core_signals

BANNER = "DATA ALREADY SEEN — not counted toward PASS"
OUT = Path("/workspace/sprint/artifacts/spec_v2") / "DATA_ALREADY_SEEN__not_counted_toward_PASS__v2_core_sanity"


def main() -> int:
    cfg = load_config("/workspace/trading-bot/config.yaml")
    capital = c_paper_from(cfg)
    panel = load_panel()
    regime = build_regime(panel)
    pre = core_signals(panel)
    start = pd.Timestamp("2023-01-01")
    end = pd.Timestamp(panel["last_closed"])
    core = run_core_path(panel, pre, regime, capital, start, end)
    bench = run_bench_path(panel, capital, start, end)
    ceq, cret = curve_series(core)
    beq, bret = curve_series(bench)
    report = {
        "banner": BANNER,
        "window": [str(start.date()), str(end.date())],
        "capital": capital,
        "V2-CORE": {
            "Sharpe": sharpe_daily_series(cret),
            "maxDD": max_drawdown(ceq),
            "end_equity": float(ceq.iloc[-1]) if len(ceq) else None,
            "n_days": int(len(cret)),
            "n_closed_trades": len(core.trades_closed),
            "killed_on": core.killed_on,
            "rearmed_on": core.rearmed_on,
        },
        "BENCH-50-50": {
            "Sharpe": sharpe_daily_series(bret),
            "maxDD": max_drawdown(beq),
            "end_equity": float(beq.iloc[-1]) if len(beq) else None,
            "n_days": int(len(bret)),
        },
        "note": "This window has already been seen (research4, reversal, spec v1). It is a plumbing/sanity check only and is not a PASS input.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    c, b = report["V2-CORE"], report["BENCH-50-50"]
    md = "\n".join([
        f"# {BANNER}",
        "",
        f"Window {report['window'][0]}–{report['window'][1]}, C_paper={capital}.",
        "",
        f"- V2-CORE: Sharpe {c['Sharpe']:.3f}, maxDD {100*c['maxDD']:.2f}%, n_days {c['n_days']}, n_trades {c['n_closed_trades']}, end {c['end_equity']:.2f}, killed_on {c['killed_on']}, rearmed {c['rearmed_on']}",
        f"- BENCH-50-50 (monthly, 2x fees): Sharpe {b['Sharpe']:.3f}, maxDD {100*b['maxDD']:.2f}%, n_days {b['n_days']}, end {b['end_equity']:.2f}",
        "",
        report["note"],
        "",
    ])
    OUT.with_suffix(".md").write_text(md)
    print(BANNER)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
