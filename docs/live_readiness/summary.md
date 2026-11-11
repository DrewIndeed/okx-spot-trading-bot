# Live readiness summary (paper / demo only)

Date: 2026-11-11, afternoon/evening UTC+7 (Etc/GMT-7).  
Worktree: `/workspace/trading-bot-live`, branch `live-ready`.  
Official spec v2 paper runner (pid 129703, `/workspace/trading-bot`) was not modified and stayed alive.

## What was built

Unattended / live-READY hardening on a separate worktree. Nothing here goes live. Default mode is paper.

1. **Pre-trade risk limits** (one gate: `okxspot.risk.manager.RiskManager.pretrade`): max position / gross exposure (existing), UTC daily-loss block on entries, max-drawdown kill-switch (existing), max orders per minute and per day, min/max notional, SPOT long only (no short entry, no margin `tdMode`, no leverage).
2. **File kill switch** (`okxspot.killswitch`, CLI `kill` / `rearm`): blocks new orders, optional cancel of open orders, persists across restarts, manual re-arm only.
3. **Watchdog** (`okxspot.watchdog`, `scripts/supervise-paper.sh`, `deploy/okxspot-paper.service`): restart with backoff, max-restarts-per-hour then trip kill switch + alert, heartbeat file. Hard-refuses pid 129703 and `/workspace/trading-bot/` pathnames.
4. **Order journal + idempotency**: deterministic `clOrdId` for (strategy, symbol, side, bar, purpose); JSONL journal; submit is idempotent; timeout/unknown status is queried before retry; partial fills, rejects, and mismatches are journaled.
5. **Stale-data guard** (`okxspot.stale`): refuses stale closed candles, candle gaps, clock skew versus `GET /api/v5/public/time`, and a large price jump versus the last bar.
6. **Alerting**: log + optional file sink by default; Telegram remains optional and token-from-env only (unset). Events: kill_switch, restart, stale_data, recon_mismatch, risk_block, daily_summary.
7. **OKX demo adapter**: `OkxClient(simulated=True)` sends `x-simulated-trading: 1`. Mock tests only. `demo-dry-run` exits 0 and explains that a demo key is needed when keys are absent.
8. **Live start gates** (still default-off): `live_enabled`, confirm env, explicit `allocated_capital`, no withdraw, IP restriction if/when detectable (fail-closed), dedicated subaccount / label / uid checks. Sizing never uses account balance.

## Tests

- tradingbot: **70 passed, 1 skipped**
- okxspot: **115 passed**
- Command: `scripts/test_all.sh` in `/workspace/trading-bot-live`

## Soak / fault injection

Time-compressed soak on 2026-11-11, 17:04–17:05 UTC+7. Evidence: `/workspace/sprint/artifacts/live_readiness/soak/` and `docs/live_readiness/soak/`.

- Stale candle → trade refused (`stale_candle`).
- Duplicate submit after a simulated crash → `place_calls=1`.
- Kill switch → new entries blocked and persisted after reload.
- Our child runner (pid 266782, not 129703) was killed with SIGKILL; supervisor relaunched (2 launches) and wrote a heartbeat.
- Official pid 129703 stayed alive at start and end.

## Demo status

Blocked on a demo key. Exact ask: an OKX demo-trading API key with read+trade and no withdraw, entered through the secret form, never chat, as `OKX_DEMO_API_KEY` / `OKX_DEMO_API_SECRET` / `OKX_DEMO_API_PASSPHRASE`.

## Open go-live items

Andrew's explicit OK; chosen capital on a dedicated subaccount (not wedding money); no-withdraw IP-restricted key via the form; a real demo run; Cypheous strategy acceptance; Sofia architecture review. Kill-switch, alert tests, and rollback steps are done or documented. Checklist: `docs/live_readiness/go_live_checklist.md`.

## How to run (this worktree only)

```bash
cd /workspace/trading-bot-live
# tests
scripts/test_all.sh
# demo dry-run (exits cleanly without a key)
.venv/bin/python -m tradingbot demo-dry-run
.venv/bin/python -m okxspot demo-dry-run --config engine/config/default.yaml
# kill switch
.venv/bin/python -m tradingbot kill --reason operator
.venv/bin/python -m tradingbot rearm
# supervisor (paper/demo; never pid 129703)
scripts/supervise-paper.sh
# systemd --user (optional)
# mkdir -p ~/.config/systemd/user && cp deploy/okxspot-paper.service ~/.config/systemd/user/
# systemctl --user daemon-reload && systemctl --user start okxspot-paper.service
```
