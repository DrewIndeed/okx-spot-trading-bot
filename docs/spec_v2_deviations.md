# Spec v2 paper-forward: deviation log

## D1 (2026-11-13, UTC+7): 1Dutc JSON sidecar written after each candle refresh

- **Symptom:** official run `spec_v2_official` failed T0 init with
  `T0 init/fill failed: Timestamp('2026-11-12 00:00:00')` (KeyError). `last_closed` stayed at
  2026-11-11, so `step_bench` had no open for the fill date 2026-11-12.
- **Cause:** `OkxRestFeed.get_candles()` persists the refreshed cache as parquet only, while the
  frozen loader `tradingbot/eval/load.py::load_panel()` reads only `data/cache/<INST>_1Dutc.json`.
  The JSON files were last written by the manual reload at 2026-11-12 11:53 UTC+7 and never
  advanced.
- **Fix:** `tradingbot/eval/v2_paper.py` now calls `_write_1dutc_json_sidecar()` after every
  `get_candles(..., "1Dutc")` refresh (BTC/ETH and the full favorites refresh). It mirrors the
  refreshed cache (all rows, confirm flag kept) into the JSON file, merges with existing rows so
  history never shrinks, and writes atomically. `load_closed_1dutc()` still drops confirm==0 bars.
- **Not changed:** spec, V2-CORE code/params, V-C code/params, `eval/load.py` and every other
  file in the v1/v2 hash sets. Freeze check after the fix: `sha256_spec_v2`,
  `sha256_v2_core_code`, `sha256_v2_core_params`, `sha256_vc_code` (d893b9a3…),
  `sha256_vc_params` (4df0cece…) all match `/workspace/sprint/artifacts/spec_v2/freeze.json`.
- **Effect on the run:** the ledger was never initialized (`initialized: false`), so the restart
  performs the normal T0 init: arm on the T0 close (2026-11-11 bar), fill at the 2026-11-12 open.
  T0 itself is unchanged. Init happens late in wall-clock time (2026-11-13) but uses the same
  closed bars it would have used on time; no pre-T0 bar is counted.
- **Test:** `tests/test_v2_paper_sidecar.py`.
- **Approvals:** Cypheous accepted the fix; Andrew approved applying it and restarting
  (2026-11-13 17:07 UTC+7). Paper only, no live.
