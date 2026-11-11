"""F2 scheduled-event calendar and HALT mask (CPI, PPI, FOMC_STATEMENT)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.strategies.spec_v1_common import (
    F2_CALENDAR_STALE_DAYS,
    F2_EVENT_TYPES,
    F2_WINDOW_HOURS,
)

CALENDAR_PATH = Path(__file__).parent / "calendars" / "events_f2_v1.csv"
FETCH_STAMP_PATH = Path(__file__).parent / "calendars" / "FETCH_STAMP.txt"
SAIGON = ZoneInfo("Asia/Saigon")


def last_fetch_timestamp() -> datetime:
    text = FETCH_STAMP_PATH.read_text()
    for line in text.splitlines():
        if line.startswith("last_successful_calendar_fetch:"):
            raw = line.split(":", 1)[1].strip()
            return datetime.fromisoformat(raw)
    raise RuntimeError("F2 calendar fetch stamp missing.")


def calendar_is_stale(now: datetime | None = None) -> bool:
    now = now or datetime.now(SAIGON)
    fetched = last_fetch_timestamp()
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=SAIGON)
    return (now - fetched).days > F2_CALENDAR_STALE_DAYS


def load_events() -> pd.DataFrame:
    df = pd.read_csv(CALENDAR_PATH)
    df = df[df["event_type"].isin(F2_EVENT_TYPES)].copy()
    df["tau"] = pd.to_datetime(df["release_datetime_utc"], utc=True)
    return df.sort_values("tau").reset_index(drop=True)


def f2_halt_mask(index: pd.DatetimeIndex, *, now: datetime | None = None) -> pd.Series:
    """True when the signal-bar close time falls in [τ − 24h, τ + 24h).

    A 1Dutc bar whose *open* is date D (00:00 UTC) closes at D+1 00:00 UTC.
    That close instant is the evaluation time ``c`` in spec §5.
    If the calendar is missing or the last successful fetch is older than 7
    calendar days, the entire mask is True (fail-safe HALT).
    """
    halt = pd.Series(False, index=index)
    if not CALENDAR_PATH.exists() or calendar_is_stale(now=now):
        halt[:] = True
        return halt
    events = load_events()
    window = pd.Timedelta(hours=F2_WINDOW_HOURS)
    # close time of bar open D is D+1 00:00 UTC
    close_times = pd.DatetimeIndex(index) + pd.Timedelta(days=1)
    close_times = close_times.tz_localize("UTC")
    for tau in events["tau"]:
        lo, hi = tau - window, tau + window  # [τ-24h, τ+24h)
        inside = (close_times >= lo) & (close_times < hi)
        halt.loc[index[inside]] = True
    return halt
