# Track T — Strategy v3 brief (hypotheses + acceptance)

**Status:** PRE-REGISTERED for screening and forward paper. Frozen for design as of 2026-11-11, 17:05 Asia/Saigon (UTC+7).  
**Author:** Cypheous (strategy acceptance). **Builder:** Tobias. Sofia reviews architecture if code changes.  
**This file does not authorize live trading, keys, or orders.** v2 stays frozen. No variant has PASSED.

**Tóm tắt:** Cả 3 biến thể v1 FAIL; v2 chỉ chạy paper từ T0 12/11/2026 07:00 (UTC+7), chưa có nến tính. Brief v3 đăng ký tối đa 5 giả thuyết (không phải 6: bỏ breadth-thrust vì trùng cửa R2 của V-A). Backtest trên dữ liệu đã thấy chỉ để chọn tối đa 3 suất paper. PASS chỉ trên paper forward, phí 2× (40 bps/lượt). Bộ lọc F2 HALT ±24h chiếm 14,33% số ngày; ngày HALT không xấu hơn ngày thường trên dữ liệu đã thấy, nên khuyến nghị nới HALT → REDUCE 0,5 như một biến thể mới — không sửa v2. 6.788 USDT tiền cưới bị loại.

SHA256 of this file must be recorded by Tobias after freeze (see §F). Official v3 T0 = the same close as v2 (**2026-11-12 07:00 UTC+7**) if this file is frozen before that close; otherwise the first 1Dutc close after freeze. The v2 runner and freeze are not to be edited.

---

## 0. Hard rules (all v3 work)

- SPOT USDT, long or flat only. No shorts, swaps, futures, margin, or leverage. Funding rates are a **signal only**; we never trade `BTC-USDT-SWAP` / `ETH-USDT-SWAP`.
- Signals on **closed** `1Dutc` candles only (`confirm == 1`). Fill at the open of bar `t+1`. No 4H variants in this brief (the 4H PB/BO holdout already failed: avgR −0.126, n=121).
- Primary cost = **2× = 40 bps per side** (0.20% fee + 0.20% slip), same lock as v1/v2. Report 1× / 25 bps / 50 bps; decide at 2×.
- Vol target **12%** annualized (log close-to-close, 90 sessions, min_periods=60, ×√365), gross cap 100%, no leverage. **Name cap 10%** on multi-name books. On the 2-name BTC/ETH book a 10% cap would make the book untradeable; pre-register a **50% name cap** there (same spirit as frozen V2-CORE, which had no 10% cap).
- Daily loss cap: closed 1Dutc equity return ≤ **−2%** → block new entries until the next session. Kill-switch: drawdown ≤ **−15%** → flatten at next open, **non-sticky** re-arm after 30 closed 1Dutc days flat AND BTC close > SMA200 (v1's sticky kill zeroed V-A/V-B holdouts; do not repeat). Peak resets on re-arm.
- Paper only. No keys, no orders, never read or print secrets. Do not touch production D1, the Cloudflare Worker, or `/workspace/crypto` (read-only). Do not overwrite `/workspace/trading-bot` or disturb the v2 paper runner (`RUNNING.lock`).
- **6,788 USDT** on OKX is Andrew's wedding money. Excluded from every path. Sizing is a percentage of a separate config `C_paper`. Never default `C_paper` from the real account or from 6,788. Wedding funds must be out of the market before **2027-01-10**.

---

## 1. Data integrity (what is already seen)

| Segment | Used by | Can it be a virgin PASS window? |
|---|---|---|
| 2023-01 → 2026-11 | research4 OOS/holdout, reversal v1 IS + v2 IS/OOS, spec v1 WF + holdouts H1/H2 (locked 2026-11-11 14:07:06 UTC+7), v2 “DATA ALREADY SEEN” sanity | **No.** Already seen. Backtests here = **screening only**, never PASS. |
| 2021-09-01 → 2023-01-01 | research4 **IS** (`research4_results.json` periods) | **No.** Used to select D1 / other research4 variants (41 variants_tested). |
| 2022 (primary) and 2021-07–12 (E_half) | supplemental 4H **OOS-2022** overlay (protocol frozen 2026-11-09 11:12:56 UTC+7). Result: every arm **INSUFFICIENT**. Protocol also states v3 proxy overlays were viewed in aggregate on 2022-07-01–2026-11-07. Reversal v1 had a descriptive 2022 H4 stress. | **No.** 2021–2022 is not a free holdout. |
| Indicator warmup through 2022-12-31 | spec v1 notes (expanding closed history before 2023-01-01 fills) | Not a scored window, but the path was read. |
| 2020 calendar year as a scored OKX window | Not a formal research4/reversal/Track T scored OOS. OOS-2022 planned E warmup from 2020-10-01; local caches for that study were described as starting 2021-11-29. This shop's BTC/ETH `1Dutc` cache starts **2021-02-03**. | **Do not treat 2020 as virgin OOS.** Depth and survivorship are unverified. |
| Forward paper from T0 | v2 official clock T0 = 2026-11-12 07:00 UTC+7; v3 shares that close if this brief is frozen first | **Yes — the only genuinely unseen scored window.** |

**There is no unused historical OKX segment left to grade v3 as PASS.** Zarattini 2015–19/03/2025 is CMC literature, not our OKX book.

Prior Track T results (context, not credit): V-A FAIL (sticky kill 2023-02-25; n=20 < 30; Sharpe −0.55 on F1 2×). V-B FAIL (2/4 folds; pooled 2× Sharpe 0.483; DSR(N=3) 0.376). V-C closest FAIL (4/4 folds, pooled 2× Sharpe 1.379 < BTC B&H 2.015, DSR 0.831, H2 Sharpe −2.739). Reversal v1: 0/12 IS. Reversal v2: 23 new trials, cumulative N=35, all three IS winners negative OOS.

---

## 2. (a) Hypotheses (k = 5; breadth-thrust dropped)

Cumulative trial count for official DSR: **N = 3 (v1) + 2 (v2) + 5 (this brief) = 10**. Every hypothesis below counts, including those that fail screening and never get a forward slot. Also *report* (do not gate) DSR with conservative **N = 45** (10 + 35 reversal trials). research4's 41 variants are a different family on overlapping data — context only, not in the official N.

Shared kill / costs / fill / wedding-money rules: §0. Do not cross these 5 with Macroy V1–V4 or with each other (no overlay grid).

**Dropped:** breadth-thrust re-entry. Production D1 / V-A already gates on breadth ≥50% of top-20 above SMA50 (R2). A thrust overlay would be a near-repeat of V-A and of v2's re-arm (30d flat + BTC > SMA200). Not worth a trial.

### Summary

| ID | Name | Base book | Expected n by T+180 (2×) | Kill |
|---|---|---|---|---|
| H1 | FUND-CROWD | V-C sleeves + v1 F2 HALT + funding crowding filter | ~25–70 multi-name round trips; should be able to reach ≥30 in a normal tape, tighter at T+90 | −15% DD, non-sticky re-arm (§0). Funding feed stale >3 calendar days: skip this filter (do not invent a rate; do not silently HALT the whole book). |
| H2 | RS-ROTATE | Standalone weekly top-5 vs BTC | ~15–40 name exits; **≥30 is possible but not guaranteed** if names are sticky — screening must verify n | −15% DD, non-sticky re-arm |
| H3 | CORE-SLOW | BTC+ETH, V2-CORE entry, slower exit | ~4–12 round trips; **material risk of n < 8 → INSUFFICIENT** | −15% DD, non-sticky re-arm |
| H4 | VOL-GATE | V-C sleeves + v1 F2 HALT + vol-level gate | ~15–80 (median split of days); should still be able to reach ≥30 if low-vol regimes are not unusually empty | −15% DD, non-sticky re-arm |
| H5 | F2-SOFT | V-C exact, except F2 is REDUCE 0.5 not HALT 0 | Same order as V-C (~30–70); slightly more entries than strict HALT | −15% DD, non-sticky re-arm |

### H1 — FUND-CROWD (funding as a crowding filter on trend entries)

- **Mechanism:** Extreme positive perpetual funding is a crowding / leverage-long signal. Spot trend entries taken into that crowding have worse squeeze risk. This is a **filter on new longs**, not a standalone alpha.
- **Rule (closed 1Dutc):** Start from frozen V-C (Donchian 20/60/90 sleeves, vol target 12%, 10% name cap, v1 F2 HALT ±24h, no force-exit on HALT). **Additionally:** at bar close `t`, read the last *settled* funding print for `BTC-USDT-SWAP` and `ETH-USDT-SWAP` whose timestamp is ≤ `t` close (no look-ahead). For a name `i`, block **new / increased** longs when that name's proxy funding ≥ the trailing **365-day 90th percentile** of its own settled prints (min_periods=90) **and** that print is **positive**. Proxy: BTC funding gates BTC and all alts; ETH funding gates ETH only. Existing positions still exit on V-C signals / stops / kill. Never place a swap order.
- **Why not a repeat:** v1 explicitly had “no funding/OI.” Reversal-v2 C3 funding was **not completed** (budget), so this is not a recycled reversal trial. Not a candle-pattern or TD9 rule. Not V-A/V-B/V-C as tested (those had no funding filter).
- **Data:** OKX public `GET /api/v5/public/funding-rate-history` (no auth; last **3 months**, limit 400, 10 req/2s) plus official archive **module 3** for 2023–2026 history. Soozie: archive module 3 timestamps are **UTC+8**; listing `sizeMB="0"` is **not** proof of an empty file (2023-01 `BTC-USDT-SWAP` zip probe had **93** rows). Do not re-download zips already probed; do not fake a User-Agent after a 403.
- **Frequency / kill:** see table. Fail-safe in the table (stale funding → skip filter).

### H2 — RS-ROTATE (relative-strength top-k vs BTC, weekly)

- **Mechanism:** Cross-sectional strength vs BTC, lagged one week, with a trend qualification so the book is long-flat rather than always fully invested. Lisaac's closest literature is short-horizon crypto momentum (Liu–Tsyvinski, 1–4 weeks; evidence mixed at monthly). Weekly rebalance is intended to keep 2× costs from eating a daily RS book.
- **Rule:** Point-in-time top-20 inside `favorites.txt` (same v1 universe rule). On each closed **1Wutc** bar, rank eligible names by 20-session *excess* close-to-close return vs BTC. Long the **top 5** that also have 1Dutc close > **SMA100**. If fewer than 2 names qualify, go flat. Weights: vol-target 12% then 10% name cap and 100% gross. Signal on the closed 1Wutc bar; **fill at the next 1Dutc open** after that close. Hold until the next weekly review. Vol-band 20% applies only to unchanged membership.
- **Why not a repeat:** V-B was *time-series* sign(1/3/12m) + Donchian 4/6 consensus, not a cross-sectional rank vs BTC. V-C is Donchian sleeves. V-A is a 20-day breakout with mid-stop and R1. Reversal v1/v2 were mean-reversion / confirmation patterns. Neighborhood risk is real (momentum literature already motivated V-B) but the rule is different and is counted as a new trial.
- **Data:** same OKX `1Dutc` / `1Wutc` public REST cache as v1. No new private endpoints.
- **Frequency / kill:** see table. If screening n < 30, it does not get a forward slot (no loosening).

### H3 — CORE-SLOW (BTC+ETH trend, slower exits)

- **Mechanism:** V2-CORE's Donchian-20 flip exits too quickly relative to a slow BTC/ETH trend. Hysteresis (enter on 2/3 sleeves, exit only when the trend is clearly gone) should cut whipsaw, at the cost of fewer trades and longer adverse holds.
- **Rule:** BTC and ETH only. **Enter** a name when 1Dutc close > SMA200 AND ≥2 of 3 Donchian (20/60/90) are long (same as V2-CORE). **Exit** that name only when close ≤ SMA200 **or** 0 of 3 Donchian sleeves are still long. Vol target 12%, 50% name cap, 100% gross, no leverage. Regime: same as frozen V2-CORE (F2 HALT ±24h + F1 REDUCE). This is a **new** variant; do not edit V2-CORE code in place.
- **Why not a repeat:** V2-CORE stays frozen (SHA in `artifacts/spec_v2/freeze.json`). This changes only the exit. Not V-A (alts, mid-stop, R1 flatten). Not V-C (multi-name 20/60/90 sleeves without the SMA200 entry gate). Not a reversal pattern.
- **Data:** BTC/ETH `1Dutc` already cached (closed bars 2021-02-03 through 2026-11-10 in the paper engine cache).
- **Frequency / kill:** see table. If n cannot reach 8 on the screening window, it is out — do not lower the floor after seeing n.

### H4 — VOL-GATE (trend only when realized vol is at or below its trailing median)

- **Mechanism:** Vol-managed overlay (Moreira–Muir style, as reviewed by Lisaac): trend-following is applied when BTC realized vol is not elevated. This is a **regime gate**, not a new signal, and is separate from 1/σ position sizing (which all vol-targeted books already use).
- **Rule:** Start from frozen V-C sleeves (including v1 F2 HALT). Let σ20 be BTC's 20-session annualized realized vol (log ret, ×√365). Let σ_med be the trailing **756-session median** of σ20 (min_periods=252). If σ20 > σ_med at close `t`: **block new / increased longs** (existing positions stay unless their own V-C exit, daily cap, or kill hits). No numeric vol cutoff other than that trailing median (do not fit 40%/50%/etc. to H2).
- **Why not a repeat:** v1/v2 used vol for *sizing*, not as an on/off gate. Not a reversal rule. **Leakage warning:** V-C's H2 (Sharpe −2.74) is already seen; a vol gate must not be justified by peeking at whether H2 was a high-vol window. The threshold is pre-registered as a trailing median for that reason. Screening uses only 2023–2024 (§3).
- **Data:** same BTC `1Dutc` cache; no macro series required for this gate.
- **Frequency / kill:** see table.

### H5 — F2-SOFT (V-C with the event HALT softened to REDUCE 0.5)

- **Mechanism / rationale:** See §4. On seen data, F2 HALT days were not systematically worse for BTC/ETH holders, and the no-F2 D1 comparator still hit a kill-switch. Strict HALT on new risk is likely too blunt; a 0.5 multiplier keeps an event caution without a hard stop on entries. This is the pre-registered event-filter change. Full *removal* of F2 was considered and **dropped** so we do not spend two adjacent trials; removal would be a new spec and would raise N.
- **Rule:** Frozen V-C code/params (SHA `d893b9a3…` / `4df0cece…`) **except** F2 state is **REDUCE 0.5** rather than HALT 0.0 inside `[τ−24h, τ+24h)`. Window width unchanged. Still no force-exit on F2 (already true in v1 and in v2: HALT = no increases; exits only via signal/stop/kill). All other V-C rules unchanged.
- **Why not a repeat:** V-C and V2-ZARA-FROZEN keep strict F2 HALT and stay frozen. This is an ablation of one multiplier, counted as a new trial. It is **not** a post-hoc regrade of v1 V-C on the same holdouts.
- **Data:** official F2 calendar already in `/workspace/trading-bot/tradingbot/eval/calendars/events_f2_v1.csv` (n=155). Refresh from BLS/Fed only if the copy becomes >7 days stale, without look-ahead on unscheduled FOMC.
- **Frequency / kill:** see table.

---

## 3. (b) Acceptance criteria (pre-registered)

**Integrity.** 2023-01 → 2026-11 is seen. Pre-2023 was also used (research4 IS from 2021-09, OOS-2022, reversal 2022 stress, indicator warmup). The only unseen scored window is forward paper from v3 T0 (§0). Any backtest on seen data must be labelled **DATA ALREADY SEEN** and **cannot count toward PASS**.

**Screening (to allocate at most 3 forward slots).** Run on **2023-01-01–2024-12-31 only**, 2× fees. Do **not** use 2025–2026, H1, or H2 to pick slots (those windows already informed v1 acceptance). A hypothesis earns a slot only if all of:

1. Official n (completed round trips with exit in the window) ≥ **30** for multi-name books (H1, H2, H4, H5) or ≥ **8** for H3 (BTC/ETH). Same justification as v1/v2: n=30 is the paper-book / reversal floor, and is a minimum not a comfort (`L_TD9_H4_G` had n=30 and still failed OOS). BTC/ETH trend is low-frequency; 8 in two years of screening is already thin, but is the same floor v2 used for 180 forward days — here it is applied to a 2-year seen window, which H3 must still meet or it is out.
2. Net-of-2× Sharpe **> 0**.
3. maxDD **better than −15%** (shallower than a 15% drawdown).

If more than 3 pass: take the 3 with highest 2× Sharpe; ties broken by shallower maxDD. That ranking is frozen before any 2025–2026 descriptive peek. If fewer than 3 pass, forward only those that passed — **do not fill leftover slots**. If none pass, v3 adds no forward book; v2 paper continues. At most one event-filter book can occupy a slot (only H5 is in that family).

**PASS (forward paper only).** Evaluate at **T+90** (interim report; DSR not a kill gate yet) and **T+180** (final), 2× = 40 bps/side, same calendar as v2's official paper (and vs a 50/50 BTC/ETH B&H book, monthly rebalance, same 2× fees):

1. **vs v2 V2-CORE (2×):** forward Sharpe ≥ V2-CORE's Sharpe on the same window. If V2-CORE is INSUFFICIENT (e.g. n < 8) or still unstarted, the comparison is reported and the floor is instead Sharpe **> 0**.
2. **vs 50/50 BTC/ETH B&H (2×):** maxDD shallower than **−15%** AND ≤ **50%** of the benchmark's maxDD over the same window. Net return after 2× **≥ 0 at T+180**. Do **not** require beating B&H Sharpe (v1 already failed that bar in a strong BTC bull; v2's risk-reduction framing stands). Return/maxDD (Calmar-like) must be ≥ the 50/50 benchmark's at T+180.
3. **Min trades at T+180:** ≥ **30** multi-name, ≥ **8** for H3. Below the floor = INSUFFICIENT, not PASS. At T+90, report n; do not fail finally on n until T+180.
4. **Deflated Sharpe / DSR** (same Bailey & López de Prado definition as research4 / v1) on forward daily returns at 2×, **N = 10**, must be **≥ 0.95 at T+180**. Report DSR at T+90 but do not gate on it (90 daily points are too short for a 10-trial haircut). Also report DSR with N=45 (context). Official N is 10, not 5, because screening itself is a trial.

Zero execution/data errors on two consecutive reports; closed candles only; no look-ahead. FAIL any final gate = FAIL. INSUFFICIENT ≠ PASS. No mid-test changes; any change is a new spec and the clock restarts. Paper success is not live permission. Live still needs Andrew's own explicit approval and a separate `C`, never the wedding funds.

v2 (`/workspace/sprint/strategy-spec-v2.md`, SHA256 `1dc04fa2ab6df8db0e5354438b80780e46fef66ad9dda2dfd32eba5573d5aed6`, freeze 2026-11-11 14:20:43 UTC+7) is **unchanged**. Do not apply H5 or any other v3 rule to V2-CORE or V2-ZARA-FROZEN.

---

## 4. (c) HALT / F2 review

**Calendar (source + stamp).** Official copy `/workspace/trading-bot/tradingbot/eval/calendars/events_f2_v1.csv`, **n=155** events (58 CPI, 57 PPI, 40 FOMC_STATEMENT), 2022-01-26 through 2026-12-09. Base rows retrieved 2026-11-09 15:26 UTC+7; four extensions (CPI 2026-11-10 already printed, PPI 2026-11-13, CPI 2026-12-10, FOMC 2026-12-09) retrieved 2026-11-11 13:55 UTC+7. Matches v2 paper_status (n=155 as of 2026-11-11 16:54:07 UTC+7). Engine rule, unchanged for this review: 1Dutc close instant `c` is HALT iff `c ∈ [τ−24h, τ+24h)`.

**Share of days (computed 2026-11-11 16:58:40 UTC+7, 1410 calendar 1Dutc bars, 2023-01-01–2026-11-10; 122 events fall in that path):**

| Window | n bars | HALT ±24h | Share | HALT ±12h | Share |
|---|---:|---:|---:|---:|---:|
| v1 path 2023-01-01–2026-11-10 | 1410 | 202 | **14.33%** | 118 | 8.37% |
| Pooled OOS 2023–2024 | 731 | 102 | 13.95% | 61 | 8.34% |
| H1 2025-03-20–2025-11-02 | 228 | 32 | 14.04% | 18 | 7.89% |
| H2 2025-11-03–2026-11-10 | 373 | 55 | 14.75% | 31 | 8.31% |

**Trades / exposure lost.** v1 and v2 already **do not force-exit** on F2: HALT = no new risk / no increases; exits stay on signal, stop, or kill (confirmed in `spec_v2_core.py`). So 14.33% is an upper bound on *entry opportunities* blocked, not on days of exposure. Trade-level blocked-entry counts for V-C are **not** in the v1 artifacts (no fill log shipped), so they are not invented here. The only strategy-level ablation already computed is the v1 **D1 no-F2 comparator** (not a scored variant): pooled OOS 2× n=31 vs V-A n=20, but both hit the sticky kill (no-F2 on 2023-04-20, V-A on 2023-02-25). Removing F2 did **not** produce a viable D1 path; it only delayed the kill and allowed 11 more trades, still with negative Sharpe (−0.21 pooled).

**Returns on HALT vs non-HALT windows (descriptive, seen data only, not a PASS input).** OKX `1Dutc` cache, `confirm==1`, last closed bar open 2026-11-10 (BTC close 73,795.8, ETH close 2,053.17; bar close 2026-11-11 07:00 UTC+7). Same-bar close-to-close returns on the v1 path:

| Book | HALT n=202 mean (sd) | non-HALT n=1208 mean (sd) | diff | descriptive t |
|---|---|---|---:|---:|
| BTC | +0.281%/d (2.76%) | +0.111%/d (2.35%) | +17.1 bp/d | 0.83 |
| ETH | +0.422%/d (3.61%) | +0.036%/d (3.22%) | +38.6 bp/d | 1.42 |
| 50/50 | +0.352%/d (3.04%) | +0.073%/d (2.66%) | +27.8 bp/d | 1.22 |

Medians are close to zero on both sides; HALT means are pulled by a fatter right tail and slightly higher vol. Negative-day share is almost the same (BTC 51.0% vs 49.3%). Next-bar returns after a HALT close are also not worse on the full path. In **H2** (the window that sank V-C), BTC HALT vs non-HALT is flat (mean −0.068% vs −0.084%, t≈0.05). These are not hypothesis tests and are not used to retune τ.

**Verdict:** Keep a scheduled-event caution, but **soften HALT → REDUCE 0.5**, keep ±24h, and keep “no force-exit” (already the rule). Do **not** patch v2. Pre-register the change as **H5** only. Narrowing to ±12h (8.37% of days) is a second degree of freedom and is not in v3. Full removal of F2 is dropped to cap trials. Evidence is descriptive and in-sample with respect to this review; H5 still has to earn a forward slot and then PASS on unseen paper.

Macro cache is **not** used as a v3 signal. For freshness only: `/workspace/crypto/macro_cache.json` `retrieved_at` 2026-11-10 20:51:21 UTC+7, verdict CAUTIOUS, official yield/DXY/VIX closes still as-of 2026-11-09. Next scheduled F2 print after T0 is PPI 2026-11-13 08:30 ET / 20:30 ICT (from the official calendar above, not from X). The T0 close itself (2026-11-12 00:00 UTC) sits after the 2026-11-10 CPI window and before the PPI window.

---

## 5. Open risks

- Survivorship: books still use the current 49-name `favorites.txt`; delisted coins are absent (stated in v1). Screening will look better than a true point-in-time life list.
- H1 funding history: REST covers only three months; longer history depends on archive module 3 (UTC+8; `0 MB` ≠ empty). C3 was never finished. A broken or stale feed must not invent prints.
- H3 may never reach n=8. H2 (RS) may miss n=30 if weekly membership is sticky.
- DSR(N=10) at T+180 is a high bar after five prior Track T variants; many economically quiet books will FAIL DSR even if DD is controlled. That is intended. Do not lower N after seeing results.
- 2× 40 bps/side may still be optimistic on thin alts (no book-impact model).
- Unscheduled FOMC 2022–2026 remains out of the calendar (look-ahead risk).
- None of these hypotheses is guaranteed to fix V-C's H2 (Sharpe −2.74). Forward paper can also fail in a similar tape.
- Do not disturb the v2 official paper runner. v3 screening/paper must be a separate run id.
- Cache/FRED/macro freshness can go stale relative to the 24h shop rule; refresh is Macroy's job, not a signal here.

---

## 6. Capability check

**Used:** `token-optimization` (batched scoped extracts, no full dumps of large JSON, no long backtest). `macro-evidence-standard` (F2 counts and HALT return gaps carry source, window, units, and retrieval time in UTC+7; no invented consensus or forecasts; cache staleness stated; HALT comparison is descriptive price context from the OKX cache, not a FRED claim). Shell / Python / `rg` for specs, v1 artifacts, v2 freeze + `paper_status.md`, `trading-regime-macroy.md`, `trading-lisaac.md`, `trading-data-soozie.md`, reversal v1/v2 reports, OOS-2022 protocol/report, `research4_results.json` periods, `events_f2_v1.csv`, read-only peek of v2 HALT behaviour, and the local `BTC/ETH-USDT_1Dutc` cache. `/workspace/cristiano/team-operating-process.md`: batched messages, `priority: true` only when the receiver must act, no bot-to-bot small talk, do not overwrite `/workspace/trading-bot`, token-opt ≤3 exploratory rounds, paper only, wedding money untouched.

**Skipped:** `x-api-mcp-guide` and namespace `x` (X is supporting news only, never a sole source; this brief has no news claim). `user-fred` (no new F3/F4 work; Macroy already specified those series). Firecrawl / WebSearch (inputs already in `/workspace`). Coding / source-control skills (no repo edit, no PR). Vault, sign-in, private OKX, Worker / wrangler / D1. SendToAgent (this turn is the file only; no bot-to-bot message). `gem_summarize.py` (markdown was cheaper to extract locally). Unrelated shopping/travel skills. No long backtest.

---

## F. Freeze and handoff for Tobias

1. SHA256 this file and write it into the v3 run manifest **before** any v3 screening HTTP or fill. Any later edit = a new brief, N must be reconsidered, clock restarts.
2. Keep v2 official paper (`spec_v2_official`, T0 2026-11-12 07:00 UTC+7) running and untouched.
3. Screening = 2023-01-01–2024-12-31, 2×, label DATA ALREADY SEEN. Allocate ≤3 forward slots by §3. Start those slots at v3 T0, separate run id, same `C_paper` convention, never the 6,788 USDT account.
4. Report format: one table per hypothesis × window × cost, first failed gate, n and n_opened, Sharpe, maxDD vs V2-CORE and vs 50/50, DSR(N=10) and DSR(N=45), regime state, errors.
5. Paper only. No keys, no orders, no live recommendation in the report.
