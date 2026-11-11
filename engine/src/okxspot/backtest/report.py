from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from okxspot import DISCLAIMER
from okxspot.backtest.metrics import (
    Metrics,
    compute_metrics,
    slice_curve,
    trades_exiting_between,
)
from okxspot.decimals import dec_str
from okxspot.models import ClosedTrade


def metrics_dict(metrics: Metrics) -> dict[str, object]:
    return {
        "cagr": dec_str(metrics.cagr),
        "max_drawdown": dec_str(metrics.max_drawdown),
        "profit_factor": dec_str(metrics.profit_factor),
        "expectancy_r": dec_str(metrics.expectancy_r),
        "win_rate": dec_str(metrics.win_rate),
        "n_trades": metrics.n_trades,
        "exposure": dec_str(metrics.exposure),
        "start_equity": dec_str(metrics.start_equity),
        "end_equity": dec_str(metrics.end_equity),
        "days": dec_str(metrics.days),
    }


def _empty_metrics() -> Metrics:
    return compute_metrics([], [], exposed_bars=0, total_bars=0)


def build_report(
    *,
    equity_curve: list[tuple[datetime, Decimal]],
    trades: list[ClosedTrade],
    exposed_bars: int,
    total_bars: int,
    oos_ratio: Decimal,
    fee_rate: Decimal,
    slippage_bps: Decimal,
    allocated_capital: Decimal,
    strategy_name: str,
) -> dict[str, object]:
    """In-sample / out-of-sample split on one continuous run.

    Parameters are not fitted on the in-sample window. The split is a holdout
    report, not evidence that a walk-forward search found an edge.
    """
    full = compute_metrics(
        equity_curve,
        trades,
        exposed_bars=exposed_bars,
        total_bars=total_bars,
    )
    count = len(equity_curve)
    cut = int(count * (1 - float(oos_ratio)))
    if count > 1:
        cut = min(max(cut, 1), count - 1)
    in_curve = slice_curve(equity_curve, 0, cut)
    out_curve = slice_curve(equity_curve, max(cut - 1, 0), count) if count else []
    in_end = in_curve[-1][0] if in_curve else None
    in_trades = trades_exiting_between(trades, None, in_end, include_start=True)
    out_trades = trades_exiting_between(trades, in_end, None, include_start=False)
    in_bars = len(in_curve)
    out_bars = max(len(out_curve) - (1 if in_curve and out_curve else 0), 0)
    folds = _folds(equity_curve, trades, oos_ratio)
    return {
        "disclaimer": DISCLAIMER,
        "strategy": strategy_name,
        "note": (
            "Placeholder or research run. Parameters were not optimized. "
            "Do not read these numbers as a forecast."
        ),
        "allocated_capital": dec_str(allocated_capital),
        "fee_rate": dec_str(fee_rate),
        "slippage_bps": dec_str(slippage_bps),
        "oos_ratio": dec_str(oos_ratio),
        "full": metrics_dict(full),
        "in_sample": metrics_dict(
            compute_metrics(in_curve, in_trades, exposed_bars=0, total_bars=in_bars)
            if in_curve
            else _empty_metrics()
        ),
        "out_of_sample": metrics_dict(
            compute_metrics(out_curve, out_trades, exposed_bars=0, total_bars=out_bars)
            if out_curve
            else _empty_metrics()
        ),
        "walk_forward_folds": folds,
        "equity_curve": [
            {"ts": ts.isoformat(), "equity": dec_str(value)} for ts, value in equity_curve
        ],
        "trades": [
            {
                "inst_id": trade.inst_id,
                "pnl": dec_str(trade.pnl),
                "r_multiple": dec_str(trade.r_multiple),
                "entry_ts": trade.entry_ts.isoformat(),
                "exit_ts": trade.exit_ts.isoformat(),
            }
            for trade in trades
        ],
    }


def _folds(
    equity_curve: list[tuple[datetime, Decimal]],
    trades: list[ClosedTrade],
    oos_ratio: Decimal,
) -> list[dict[str, object]]:
    count = len(equity_curve)
    if count < 4:
        return []
    oos_len = max(1, int(count * float(oos_ratio)))
    is_len = max(1, count - oos_len)
    folds: list[dict[str, object]] = []
    start = 0
    while start + is_len + oos_len <= count and len(folds) < 12:
        is_end = start + is_len
        oos_end = is_end + oos_len
        oos_curve = equity_curve[is_end - 1 : oos_end]
        start_ts = equity_curve[is_end - 1][0]
        end_ts = equity_curve[oos_end - 1][0]
        oos_trades = trades_exiting_between(trades, start_ts, end_ts, include_start=False)
        metrics = compute_metrics(
            oos_curve,
            oos_trades,
            exposed_bars=0,
            total_bars=max(len(oos_curve) - 1, 0),
        )
        folds.append(
            {
                "oos_start": start_ts.isoformat(),
                "oos_end": end_ts.isoformat(),
                "metrics": metrics_dict(metrics),
            }
        )
        start += oos_len
    return folds


def write_equity_png(points: list[tuple[datetime, Decimal]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4))
    if points:
        ax.plot(
            list(range(len(points))),
            [float(point[1]) for point in points],
        )
        labels = [point[0].date().isoformat() for point in points]
        if len(labels) > 1:
            ax.set_xticks([0, len(labels) - 1])
            ax.set_xticklabels([labels[0], labels[-1]])
    else:
        ax.text(0.5, 0.5, "no equity points", ha="center", va="center")
    ax.set_title("Equity curve (research only — not a performance claim)")
    ax.set_ylabel("Bot equity (USDT)")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write_backtest_report(report: dict[str, object], directory: Path) -> tuple[Path, Path]:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "backtest.json"
    png_path = directory / "equity.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    curve_raw = report.get("equity_curve")
    points: list[tuple[datetime, Decimal]] = []
    if isinstance(curve_raw, list):
        for item in curve_raw:
            if isinstance(item, dict):
                points.append(
                    (
                        datetime.fromisoformat(str(item["ts"])),
                        Decimal(str(item["equity"])),
                    )
                )
    write_equity_png(points, png_path)
    return json_path, png_path
