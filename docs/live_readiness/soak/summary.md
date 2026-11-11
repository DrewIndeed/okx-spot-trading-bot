# Soak evidence (time-compressed)

- Started: 2026-11-11T17:04:32.601189+07:00 (UTC+7 / Etc/GMT-7)
- Ended: 2026-11-11T17:05:13.314681+07:00 (UTC+7 / Etc/GMT-7)
- Worktree: `/workspace/trading-bot-live`
- Official pid 129703 alive at start: True
- Official pid 129703 alive at end: True
- Paper pretrade stretch: 40.0s

## Faults
- Stale data: refused with `stale_candle`. See `excerpts/stale.log`.
- Duplicate submit: place_calls=1. See `excerpts/duplicate.log`.
- Kill switch: blocked and persisted. See `excerpts/kill_switch.log`.
- Runner kill/restart: launches=2. See `excerpts/restart.log`.

All faults were handled. This is paper/demo only. No live orders were sent.
