# Spec v3 paper-forward: deviation log

## D3 (2026-11-21, UTC+7): F2 macro calendar refreshed (data staleness only)

- **Symptom:** after the 2026-11-21 restart `spec_v3_official` reported `F2 calendar fetch stamp is
  older than 7 days (fail-safe HALT would apply)` every iteration and did not step any book
  (all books stayed at last_processed 2026-11-12).
- **Fix (data only):** same 33 appended rows and FETCH_STAMP as v2 D3 / live-ready-cursorfix
  52f530f (BLS CPI/PPI schedules, Fed 2027 FOMC days). Existing rows and CRLF endings untouched.
- **Not changed:** `calendar_is_stale()`, the 7-day limit, all v3 code and params.
  `sha256_v3_code` 533ee52e… and slot params still match `/workspace/sprint/artifacts/spec_v3/freeze.json`
  (the v3 freeze does not hash the calendar). The V-C comparator's v1 hash set reads the v2 tree
  calendar and moves d893b9a3 → 2db0d972 under v2 D3.
- **Effect:** the missed 2026-11-13…19 closes are stepped on restart with the real F2 mask; no
  state restore was needed because nothing had been stepped while stale.
- **Approvals:** Andrew via Cris (2026-11-21 00:30 UTC+7). Paper only, no live.
