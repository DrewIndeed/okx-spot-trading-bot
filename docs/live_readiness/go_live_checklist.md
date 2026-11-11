# GO-LIVE checklist (OKX SPOT)

This engine is **paper / OKX demo only** until every row below is marked done with evidence. Default mode is paper. The 6,788 USDT on the main account is wedding money and must never be used for sizing or funding.

Worktree: `/workspace/trading-bot-live` (branch `live-ready`).  
Do not use `/workspace/trading-bot` or pid **129703** (official spec v2 paper-forward run).

| # | Gate | Status | Evidence |
|---|------|--------|----------|
| 1 | Andrew's explicit OK in chat to go live | **OPEN** | No chat approval has been given. Code still refuses live unless `live_enabled: true`, `--enable-live`, and `OKX_LIVE_CONFIRM=I_ACCEPT_LIVE_TRADING_LOSS_RISK` or `LIVE_CONFIRM=I_ACCEPT` are all set. |
| 2 | Capital chosen separately by Andrew, not the 6,788 USDT wedding money, on a dedicated subaccount | **OPEN** | Sizing reads only `allocated_capital` / `max_allocated_capital`. Live start refuses the main account (`uid == mainUid`) when `require_dedicated_subaccount: true` (default). Andrew has not yet named a live allocation or subaccount. |
| 3 | No-withdraw, IP-restricted API key entered only through the hidden secret form (never chat) | **OPEN** | Live start calls `GET /api/v5/account/config` and refuses withdraw permission, a missing/empty `ip` field, or an undetectable restriction. No key has been supplied. Env names are listed empty in `.env.example` and `engine/.env.example`. |
| 4 | OKX demo-trading run passed | **BLOCKED** | `okxspot demo-dry-run` and `python -m tradingbot demo-dry-run` exit 0 and print that a demo key is needed when `OKX_DEMO_API_*` are unset. Mock unit tests cover the `x-simulated-trading: 1` header. A real demo run needs an OKX **demo-trading** API key with **read+trade and no withdraw**, entered through the secret form, never chat. |
| 5 | Strategy acceptance by Cypheous | **OPEN** | Spec v2 remains the frozen baseline on the official paper runner. No v3/live strategy acceptance has been recorded. |
| 6 | Architecture review by Sofia | **OPEN** | Not yet requested/completed for this live-ready worktree. |
| 7 | Kill-switch test | **DONE** | Unit tests in `engine/tests/test_kill_switch.py` (persist, no auto re-arm, optional cancel). Soak: `docs/live_readiness/soak/excerpts/kill_switch.log` — entry allowed, then blocked, state persisted after reload. CLI: `okxspot kill` / `tradingbot kill` and `rearm`. |
| 8 | Alert test | **DONE** | `engine/tests/test_ops_alerts_cli.py` writes kill_switch, restart, stale_data, recon_mismatch, risk_block, daily_summary to a file sink. Telegram stays a stub unless `TELEGRAM_BOT_TOKEN` is set (unset now; token never logged). Soak alerts: `docs/live_readiness/soak/alerts.jsonl`. |
| 9 | Rollback steps | **DOCUMENTED** | See below. Not yet executed against a live session (no live session exists). |

## Rollback steps

1. Trip the kill switch immediately: from this worktree, ` .venv/bin/python -m tradingbot kill --reason rollback --cancel-open ` or ` .venv/bin/python -m okxspot kill --config engine/config/default.yaml --reason rollback --cancel-open `. This blocks new orders and persists across restarts. Re-arm is manual only.
2. Stop the supervisor and any paper/demo child started from `/workspace/trading-bot-live`. Do **not** signal pid 129703 or anything under `/workspace/trading-bot/`.
3. If a demo or (future) live session had open orders, confirm they were cancelled (`--cancel-open` or cancel on the OKX demo/live UI). Do not place flattening live orders from this bot unless Andrew has already approved live and the session is the dedicated subaccount.
4. Leave `live_enabled: false` (the default). Unset `OKX_LIVE_CONFIRM` / `LIVE_CONFIRM`. Do not reuse a withdraw-enabled or non-IP-restricted key.
5. Keep the official spec v2 paper runner (`spec_v2_official`, pid 129703) running unless Andrew or Cris explicitly says otherwise.

## Demo key ask (exact)

Andrew would need to create an **OKX demo-trading** API key with **read + trade**, **withdraw disabled**, preferably IP-restricted, and enter `OKX_DEMO_API_KEY`, `OKX_DEMO_API_SECRET`, and `OKX_DEMO_API_PASSPHRASE` through the hidden secret form. Never paste a key in chat.
