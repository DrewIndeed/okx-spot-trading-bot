"""Append-only paper log conforming to data-contract.md §7 (type=init / type=day).

Equities are stored in USDT (allocated_capital notional) and as a relative multiple
so the log remains comparable to the older 1.0-start paper_book_log.jsonl.
Idempotent: a type=day record is appended only if that UTC date is new and <= ready.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


SPEC = (
    "tradingbot T1: sleeves=strategy variants (<=3); "
    "D1 = REF_d1_a4_soozie (ported research4.a4_soozie mid-stop); "
    "placeholder = PLACEHOLDER_ema_cross_atr; "
    "capital=allocated_capital (dedicated sub-account, never OKX main balance)"
)


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def last_day(records: list[dict]) -> Optional[str]:
    days = [r["date"] for r in records if r.get("type") == "day" and r.get("date")]
    return max(days) if days else None


def append_jsonl(path: Path | str, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def closed_trade(*, sleeve: str, coin: str, entry: float, exit: float, fee_side: float, slip_side: float) -> dict:
    """Schema from paper_book.trades_from, with explicit fee/slip instead of a hardcoded 0.998**2.

    ret_pct uses both sides: exit/entry * (1-fee-slip)**2 - 1, equivalent when fee+slip=0.002.
    """
    cost = (1.0 - fee_side - slip_side) ** 2
    return {
        "sleeve": sleeve,
        "coin": coin,
        "entry": entry,
        "exit": exit,
        "ret_pct": (exit / entry) * cost - 1.0 if entry else None,
    }


def make_init(
    *,
    date: str,
    allocated_capital: float,
    sleeve_weights: dict,
    positions: dict,
    s1_stops: dict,
    state_as_of_close: str,
    spec: str = SPEC,
) -> dict:
    rel = 1.0
    return {
        "type": "init",
        "date": date,
        "created": time.strftime("%Y-%m-%d %H:%M %z"),
        "allocated_capital": allocated_capital,
        "book_equity": allocated_capital * rel,
        "book_equity_rel": rel,
        "d1_equity": allocated_capital * rel,
        "d1_equity_rel": rel,
        "state_as_of_close": state_as_of_close,
        "sleeve_weights": sleeve_weights,
        "positions": positions,
        "s1_stops": s1_stops,
        "spec": spec,
    }


def make_day(
    *,
    date: str,
    allocated_capital: float,
    sleeve_weights: dict,
    positions: dict,
    book_ret: float,
    d1_ret: float,
    book_equity: float,
    d1_equity: float,
    book_trades_closed: list,
    d1_trades_closed: list,
) -> dict:
    cap = allocated_capital or 1.0
    return {
        "type": "day",
        "date": date,
        "allocated_capital": allocated_capital,
        "sleeve_weights": sleeve_weights,
        "positions": positions,
        "book_ret": book_ret,
        "d1_ret": d1_ret,
        "book_equity": book_equity,
        "book_equity_rel": book_equity / cap,
        "d1_equity": d1_equity,
        "d1_equity_rel": d1_equity / cap,
        "book_trades_closed": book_trades_closed,
        "d1_trades_closed": d1_trades_closed,
    }


def write_init_if_needed(path: str | Path, record: dict) -> bool:
    p = Path(path)
    recs = _read_all(p)
    if any(r.get("type") == "init" for r in recs):
        return False
    append_jsonl(p, record)
    return True


def write_day_idempotent(path: str | Path, record: dict, ready_date: str) -> bool:
    """Append type=day only if date is after the last logged day and <= ready_date (UTC dates ISO)."""
    p = Path(path)
    recs = _read_all(p)
    prev = last_day(recs)
    d = record["date"]
    if d > ready_date:
        return False
    if prev is not None and d <= prev:
        return False
    append_jsonl(p, record)
    return True
