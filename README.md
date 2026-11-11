# tradingbot — OKX spot paper / dry-run engine (T1 scaffold)

SPOT-only trading engine skeleton for OKX. Default mode is **dry_run**. Live orders are **not implemented** and are always refused. Strategy-spec v1 from Cypheous is not wired yet; plugins are pluggable.

This engine does **not** claim profitability. Backtest numbers are placeholder / reference-plugin results with an in-sample / out-of-sample walk-forward split.

## Architecture

```
CLI (backtest | paper-once | ws-smoke)
  ├─ config.yaml + env          mode, fees, allocated_capital (required)
  ├─ data.MarketDataFeed        OKX public REST candles (cached) + public WS
  ├─ universe / breadth         favorites.txt (49 USDT pairs); monthly PIT top-20 helper
  ├─ regime                     BTC close > SMA200 AND breadth >= 50% (new D1 entries)
  ├─ strategy plugins (≤3 side by side)
  │    ├─ PLACEHOLDER_ema_cross_atr     EMA cross + ATR stop (not an edge)
  │    └─ REF_d1_a4_soozie              port of research4.a4_soozie / daily_bo (reference)
  ├─ risk manager               0.5–1% risk, daily cap, max-DD kill switch, exposure,
  │                             max open positions, duplicate-order guard (signal id / clOrdId)
  ├─ order manager              paper fill with fee + slippage; no live path
  ├─ portfolio state            JSON, load on start, survives restart
  ├─ paper log                  JSONL type=init / type=day (data-contract §7)
  └─ alerts                     Telegram stub (logs only; never sends)
```

Data-layer interface (`tradingbot.data.base.MarketDataFeed`, `Candle`) is intentionally narrow so Cypheous's data contract / strategy-spec v1 can be adapted without a rewrite.

## How to run

Use the project venv (stdlib + requests / pandas / numpy; pytest is in the venv):

```bash
cd /workspace/trading-bot
.venv/bin/python -m pytest -q
.venv/bin/python -m tradingbot backtest --no-update
# 4H is the default primary bar; 1H is optional/secondary:
.venv/bin/python -m tradingbot backtest --bar 1H --strategies placeholder
.venv/bin/python -m tradingbot paper-once --strategy d1 --no-update
.venv/bin/python -m tradingbot ws-smoke
```


```bash
# Historical pipeline (4H + 1Dutc; 1H optional). Incremental REST, parquet cache, quality report:
.venv/bin/python -m tradingbot data-sync
# .venv/bin/python -m tradingbot data-sync --include-1h   # optional secondary bar

# One paper iteration (proves the loop body). Do not leave the 24h runner up unless asked:
.venv/bin/python -m tradingbot paper-once --strategy d1
.venv/bin/python -m tradingbot paper-status
# .venv/bin/python -m tradingbot paper-start --strategies d1 --poll 60
# .venv/bin/python -m tradingbot paper-stop
```

Cache is parquet (pyarrow in `.venv`). If pyarrow is missing the engine falls back to CSV and says so. Quality reports: `data/quality_report.json` and `data/quality_report.md`.


Up to three strategy variants can run side by side, e.g. `--strategies placeholder,placeholder_slow,d1`. Each variant is also reported under a **2× fee stress** (fee doubled; slippage scales with fee).

Default backtest: walk-forward on **BTC-USDT 4H** (placeholder) plus the **D1 1Dutc** reference plugin on `/workspace/crypto/favorites.txt`. Closed candles only (`confirm == 1`).

## Safety rules

- **SPOT long / USDT only.** No shorts, no futures/swap, no withdraw, no production D1 / Cloudflare Worker changes.
- **Never place live orders.** `mode` defaults to `dry_run`. Live requires `--live` **and** `LIVE_APPROVED=1` **and** `mode: live`, and **still raises `NotImplementedError`**. `OrderManager.submit_live` always refuses.
- **Main OKX account balance is off-limits.** The 6,788 USDT Simple Earn figure (wedding money) and any other wallet / Earn / staking / loan balance must not be used as capital. `okx_balance_readonly.py` and `okx_onchain_readonly.py` are **not** wired in.
- **`allocated_capital` / `paper_notional_usdt` is required** in config (explicit number, e.g. 10,000). There is no default from account balance, from `OKX_*` env, or from 6,788. Missing or non-positive values raise. All paper equity and sizing start from this dedicated **sub-account** allocation.
- **Never read, request, or print API keys or secrets** (`OKX_*`, Telegram tokens, passphrases). The Telegram hook only logs; it does not send, even if a token is present (no external send without Andrew).
- **Public market data only** (OKX REST `/market/candles`, `/market/history-candles`, `/market/ticker`, and the public WebSocket). No private endpoints.
- **Closed candles only** (`confirm == 1`) for signals and sizing.
- **Do not modify anything under `/workspace/crypto`.** Import or copy logic only. This tree caches under `data/cache/` (seeded *read-only* from the existing crypto cache).
- Do not treat placeholder or D1 reference results as a proven edge. Prior reversal v1/v2 failed. Go-live is Andrew's T3 decision only, on an isolated sub-account after an explicit OK.

## Costs

Base: OKX spot taker **0.1% / side**. Slippage default = **2× fee** (0.2% / side). Stress run uses **2× fee** (slippage scales). This is slightly more conservative than the research `COST = 0.2% / side` combined figure. 4H / 1H placeholder fills use next-bar open with stop-first exits (same conservative rule as `/workspace/crypto/backtest.py`). D1 uses close-to-close turnover costs, matching `research4.a4_soozie` timing.

## Universe and regime

- Trading universe = `/workspace/crypto/favorites.txt` (49 USDT pairs, including **HYPE** and **ONDO**). Not a live “top-20 by volume” list.
- Breadth is computed on a **point-in-time monthly top-20** (30-day average quote volume, cache field `[6]`; eligibility ≥200 days and 30d median quote vol ≥ $2M; recomputed on calendar day 1 with data at `t-1`).
- D1 new-entry regime helper: **BTC close > SMA200 AND breadth ≥ 50%** of that top-20 above SMA50. A BTC close ≤ SMA200 flattens D1 alt holds (R1). This is not the 4H EMA200 filter in `regime.py`.

## What was reused from `/workspace/crypto` (copy / read, not modified)

- `data.py` — public REST fetch, throttle, history backfill, candle parse `[ts,o,h,l,c,vol,volCcy,confirm]`
- `ind.py` — causal EMA (SMA-seeded) and Wilder ATR
- `backtest.py` — fee application, next-open entry, stop-first conservative fill
- `research4.py` `universe` / `breadth` / `a4_soozie` / `cap_total` — copied into helpers + D1 plugin
- `daily_bo.py` — wrapper path documented; plugin follows `a4_soozie(..., 'mid')`
- `paper_book.py` — JSONL `type=init` / `type=day` schema
- `favorites.txt` — universe
- Existing `data/*_4H.json` and `*_1Dutc.json` — **read-only seed** into `data/cache/`
- `paper_book.py` persistence idea (JSON / JSONL)
- `regime.py` was **not** imported (it can pull CoinGecko via `matrix`); SMA200 gate was re-implemented from the contract / `research4.g_S1`

## Open TODOs

- Hook Cypheous strategy-spec v1 (expected under `/workspace/sprint/strategy-spec.md`) into the plugin interface.
- 24h paper trading on OKX demo, with public WS + paper log, once the spec is in.
- Telegram alerts: token not configured; stub only, no send.
- Optional 4H setup path (`strat.signal` + EMA200 filter) is not a default plugin here.


## Paper forward runtime

The official spec v2 paper-forward clock uses **this package's `tradingbot` paper runner**, not `okxspot`.

**Why:** `engine/okxspot` is a single-strategy paper/live engine with a Donchian-ATR placeholder, one book, and no F2 calendar, V-C (49-name) book, or 50/50 monthly BTC/ETH benchmark ledger. Porting all of that before T0 would be a significant rework. The `tradingbot` runner already does REST polling of `confirm==1` candles, has start/status/stop + pidfile + restartable state, and can host the three official books. `okxspot live` is never enabled from this path (no `--enable-live`, no `OKX_LIVE_CONFIRM`).

Official T0 = 1Dutc close at 2026-11-12 07:00 UTC+7. Bars before T0 are not counted. Paper only. Sizing = config `C_paper` (never the OKX account; 6,788 USDT wedding funds excluded).

```bash
# from /workspace/trading-bot
# Official run (own run_id / state / logs):
.venv/bin/python -m tradingbot paper-start --strategies v2 --run-id spec_v2_official --poll 60
.venv/bin/python -m tradingbot paper-status --run-id spec_v2_official
.venv/bin/python -m tradingbot paper-stop  --run-id spec_v2_official

# A later/alternate unofficial smoke run (separate state dir; does not affect the official clock):
.venv/bin/python -m tradingbot paper-start --strategies v2 --run-id SMOKE-unofficial-until-Andrew-approves-v2 --poll 60
```

Logs and status:

- pid / runner status: `state/spec_v2/spec_v2_official/paper.pid`, `state/spec_v2/spec_v2_official/paper_status.json`
- ledger: `state/spec_v2/spec_v2_official/ledger.json`
- runner log: `logs/spec_v2/spec_v2_official/paper_runner.log`
- §7 paper status: `/workspace/sprint/artifacts/spec_v2/spec_v2_official/paper_status.md` and `paper_status.jsonl` (copies at `/workspace/sprint/artifacts/spec_v2/paper_status.md`)
- freeze: `/workspace/sprint/artifacts/spec_v2/freeze.json` (lock.json is a copy)

Both test suites (tradingbot + okxspot):

```bash
./scripts/test_all.sh
# or: make test-all
```

## Tests

` /workspace/trading-bot/.venv/bin/pytest -q ` covers risk (sizing, caps, kill-switch, duplicates, allocated_capital), order fees/slippage, live refusal, portfolio restart, WS mock + optional live smoke, universe/breadth/regime, D1 plugin sizing, and paper-log schema.

## Live readiness (paper / OKX demo only)

Hardening for unattended overnight paper/demo runs lives on branch `live-ready` in worktree `/workspace/trading-bot-live`. It must not be mixed with the official spec v2 paper-forward runner (pid 129703, `/workspace/trading-bot`).

This tree is **not live**. Default mode is paper. Live start is refused unless all of these hold: `live_enabled: true` in config, `--enable-live`, `OKX_LIVE_CONFIRM=I_ACCEPT_LIVE_TRADING_LOSS_RISK` or `LIVE_CONFIRM=I_ACCEPT`, an explicit `allocated_capital` (never the 6,788 USDT wedding money, never the account balance), a trade-only key with **no withdraw**, IP restriction when detectable (fail-closed), and a dedicated subaccount/label check. Andrew's explicit OK in chat is still required before anyone turns those flags on.

### Operations in this worktree

```bash
cd /workspace/trading-bot-live
scripts/test_all.sh
.venv/bin/python -m tradingbot demo-dry-run
.venv/bin/python -m tradingbot kill --reason operator      # persist; blocks new orders
.venv/bin/python -m tradingbot rearm                       # manual only
scripts/supervise-paper.sh                                 # watchdog; never touches pid 129703
```

The supervisor writes a heartbeat (`state/heartbeat.json` by default), restarts the child with backoff, and trips the kill switch + alerts if restarts exceed the hourly cap. A systemd --user unit is in `deploy/okxspot-paper.service` (WorkingDirectory is this worktree only).

Pre-trade limits, the stale-data guard (including clock skew versus `GET /api/v5/public/time`), the order journal, and file/log alerts are in `engine/src/okxspot/`. Telegram remains a stub until `TELEGRAM_BOT_TOKEN` is set via the secret form.

### OKX demo trading

`OkxClient(simulated=True)` sends the official `x-simulated-trading: 1` header. Tests are mock-based. `demo-dry-run` exits 0 and tells you a demo key is needed if `OKX_DEMO_API_KEY`, `OKX_DEMO_API_SECRET`, and `OKX_DEMO_API_PASSPHRASE` are unset.

To run a real demo session, Andrew would need to create an **OKX demo-trading** API key with **read + trade** and **withdraw disabled**, then enter those three variables through the hidden secret form, never chat.

### Checklist and soak evidence

- Go-live checklist (evidence column, open items): `docs/live_readiness/go_live_checklist.md`
- Summary: `docs/live_readiness/summary.md`
- Time-compressed soak + fault-injection excerpts: `docs/live_readiness/soak/`
