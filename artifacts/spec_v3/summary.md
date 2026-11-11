# Spec v3 screening summary

DATA ALREADY SEEN — screening only, not counted toward PASS

Written: 2026-11-11 17:11:35 +0700 (Asia/Saigon, UTC+7)
Official brief: `/workspace/sprint/strategy-v3-brief.md` SHA256 `6544095cf2e7203935e9741f3bfaef139afc3fddee0676f3c6b364f7dbbd72d2`
Cypheous owns strategy acceptance. This is screening on seen data only. **Not a PASS.**
Paper only. No keys, no live orders, no forward runner started. 6,788 USDT wedding funds excluded. C_paper=10000.

## Honest out-of-sample status

- 2023-01 → 2026-11 has already been seen (research4, reversal, spec v1 WF + holdouts, v2 sanity).
- Pre-2023 was also used (research4 IS, OOS-2022, warmup). There is no unused historical OKX segment that can grade v3 as PASS.
- Screening used **2023-01-01–2024-12-31 only**, 2× = 40 bps/side. 2025–2026 and v1 holdouts H1/H2 were **not** used to choose slots.
- The only clean OOS is forward paper from v3 T0 (same close as v2, 2026-11-12 07:00 UTC+7, if the freeze is accepted before that close). That runner is **not** started here.

## Baseline replication (frozen code, official cache)

Window 2023-01-01–2026-11-10 (v2 sanity overlap), C_paper=10000.0.
Loader: frozen tradingbot.eval.load.load_panel (official /workspace/trading-bot JSON cache).
- V2-CORE: Sharpe 0.635070, maxDD -7.5284%, n_days 1410, n_trades 21, end 12025.1056
- BENCH-50-50: Sharpe 0.793016, maxDD -60.1836%, n_days 1410, end 29227.0360
- Replication vs v2 sanity file: MATCH.

## Official hypotheses (k=5). Official DSR N=10 (3 v1 + 2 v2 + 5). Also report N=45.

sr_trials_var (unannualized daily Sharpes of the 10 trials) = 0.00077955.

| ID | Name | n | Sharpe | maxDD | PF | end eq | DSR(N=10) | DSR(N=45) | screen | first failed gate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| H1 | FUND-CROWD | 157 | 1.361 | -14.18% | 2.06 | 13430.1 | 0.770 | 0.595 | PASS | — |
| H2 | RS-ROTATE | 148 | 1.116 | -16.47% | 2.34 | 12828.9 | 0.655 | 0.456 | FAIL | maxDD=-16.4694% not shallower than -15% |
| H3 | CORE-SLOW | 10 | 0.813 | -7.51% | 2.33 | 11596.4 | 0.484 | 0.293 | PASS | — |
| H4 | VOL-GATE | 129 | 1.196 | -10.15% | 2.01 | 12565.6 | 0.694 | 0.502 | PASS | — |
| H5 | F2-SOFT | 160 | 1.406 | -14.04% | 2.03 | 13623.1 | 0.790 | 0.619 | PASS | — |

A hypothesis passes screening only if n≥30 (≥8 for H3), Sharpe>0, and maxDD shallower than −15%, all at 2× on 2023–2024.

## Forward slots (≤3)

H5, H1, H4 (ranked by 2× Sharpe, then shallower maxDD).
Do not start a paper runner until Cypheous signs off on this screening result and the freeze hashes.

## Comparators on the same 2023–2024 window (2×), not slot candidates

- V2-CORE: Sharpe 0.949, maxDD -7.47%, n=12
- 50/50 BTC/ETH monthly: Sharpe 1.639, maxDD -35.64%
- V-C frozen (v1 engine, sticky kill): Sharpe 1.379, maxDD -14.19%, n=160

## Funding (H1)

OKX official archive module 3 (monthly zips) plus public REST ~3 months (2026 only, not used in this window)
funding_time is a Unix epoch instant; module 3 file months are UTC+8 (2023-01 zip starts 2022-12-31 16:00 UTC = 2023-01-01 00:00 UTC+8). No extra 8h shift.
BTC prints: 3239 from ['2022-01-16 16:00:00', '2024-12-31 08:00:00'].
ETH prints: 3239 from ['2022-01-16 16:00:00', '2024-12-31 08:00:00'].
Screening days with BTC crowding: 100; ETH crowding: 95; BTC stale-skip: 0.
copied from /workspace/crypto/reversal-v2/raw/funding/BTC_2023-01.zip (not re-downloaded).

## Freeze

Hashes: `/workspace/sprint/artifacts/spec_v3/freeze.json`. No overwrite. No paper runner started from this step.

Monte Carlo (5,000 trade-order reshuffles, seed 42) and regime breakdowns (BULL/BEAR/CHOP) are in `screening.json`.
Stress costs 1× / 25 bps / 50 bps are reported there; selection used 2× only.

## Monte Carlo caveat

Trade-order reshuffles (5,000, seed 42) compound each closed trade's *per-name* `pnl_frac`. The product of `(1+r)` is commutative, so **final equity is invariant to order** and the equity percentiles are not informative. The maxDD distribution from that stack is also **not** the book maxDD: it treats each name-trade as a 100% book, ignoring vol-target weights and overlapping holds, and is much worse than the path maxDD used for screening. Path maxDD in the table above is the official number.

## H3

CORE-SLOW passed screening (n=10 ≥ 8, Sharpe 0.813, maxDD −7.51%) but ranked 4th on Sharpe and did not receive a slot (cap 3).


## Data coverage (snapshot 2026-11-11, UTC)

- Seed: 125 USDT-spot names (favorites ∪ historical majors ∪ OKX top-40 volume snapshot, stables/wrapped excluded). H2 PIT top-20 is inside `favorites.txt` only.
- 1Dutc: 125 series, closed bars 57–3007, earliest 2018-08-18 00:00 UTC, latest 2026-11-10 00:00 UTC. BTC/ETH: 3007 closed bars from 2018-08-18.
- 4H: 113 series at snapshot (backfill still running), earliest 2019-01-22 12:00 UTC, latest 2026-11-11 04:00 UTC.
- 1H: not yet complete at snapshot (backfill still running). Screening used 1Dutc only (brief: no 4H variants).
- Manifest with SHA256 checksums: `artifacts/spec_v3/data/manifest.json`. Cache is gitignored.
- Funding: 3239 settled prints each for BTC-USDT-SWAP and ETH-USDT-SWAP, 2022-01-16 16:00 UTC → 2024-12-31 08:00 UTC, from official archive module 3. The 2023-01 BTC zip was copied from the earlier probe, not re-downloaded.
