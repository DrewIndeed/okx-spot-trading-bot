# OKX Spot Trading Bot — Windows Runbook

Repo: `C:\Users\ADMIN\Documents\GitHub\okx-spot-trading-bot`
Python: 3.14.3 (venv at `.venv\`)

## Prerequisites

- Git Bash (from Git for Windows) — recommended terminal
- Python 3.14 already installed at `C:\Users\ADMIN\AppData\Local\Programs\Python\Python314\`
- Internet access — bot fetches public OKX market data (REST + WebSocket), no API key needed for dry-run

## One-Time Setup

Run once in Git Bash inside the repo folder:

```bash
cd /c/Users/ADMIN/Documents/GitHub/okx-spot-trading-bot

# Remove old venv (if any) and create fresh
rm -rf .venv
py -3.14 -m venv .venv
source .venv/Scripts/activate
export PYTHONUTF8=1

# Upgrade pip and install the bot in editable mode
python -m pip install -U pip
pip install -e .

# Create .env from example (paper/dry-run only, no keys needed)
cp .env.example .env
```

Every time you open a new Git Bash window, repeat:

```bash
cd /c/Users/ADMIN/Documents/GitHub/okx-spot-trading-bot
source .venv/Scripts/activate
export PYTHONUTF8=1
```

## Config (`config.yaml`)

Already patched for Windows. Key values:

| Key | Value | Notes |
|-----|-------|-------|
| `mode` | `dry_run` | Live orders always refused |
| `allocated_capital` | `10000` | Virtual paper capital, NOT OKX main balance |
| `paper_notional_usdt` | `10000` | Same as allocated_capital |
| `universe_file` | `data/universe_seed.txt` | Local coin list (125 USDT pairs) |
| `crypto_data_dir` | `data` | Read-only seed dir (relative, Windows-safe) |

Do not point `universe_file` or `crypto_data_dir` at `/workspace/...` — those are Linux build-machine paths that don't exist on Windows.

## Commands

All commands run from the repo folder with the venv activated.

### 1. Download market data (public OKX REST only)

```bash
python -m tradingbot data-sync --bars 4H,1Dutc
```

- Fetches closed candles for all 125 universe symbols
- Cache written to `data/cache/` (parquet, or CSV fallback)
- Can take several minutes for all symbols; safe to interrupt and re-run (incremental)
- Add `--no-update` to rebuild report from cache only (no REST calls)
- Add `--include-1h` to also fetch 1H bars

### 2. Backtest (walk-forward, IS/OOS)

```bash
# Default: placeholder + D1 reference, BTC-USDT 4H
python -m tradingbot backtest --no-update --strategies placeholder,d1

# Include my_d1 (unlocked tunable strategy)
python -m tradingbot backtest --no-update --strategies placeholder,d1,my_d1

# Just my_d1, fewer days for speed
python -m tradingbot backtest --no-update --strategies my_d1 --days 600

# 2x fee stress is included automatically in the report
```

- Max 3 strategy variants: `placeholder`, `placeholder_slow`, `d1`, `my_d1`
- `my_d1` is the unlocked book: lookback 20, exposure_scale 0.50. Not a proven edge.
- Results are placeholder/reference only — not a profitability claim
- Look at **Pooled OOS** line, not Full-window

### 3. One paper cycle

```bash
python -m tradingbot paper-once --strategy d1 --no-update
python -m tradingbot paper-once --strategy placeholder --no-update
python -m tradingbot paper-once --strategy my_d1 --no-update
```

- Runs one iteration on the last closed bar
- Writes state to `state/portfolio.json` and log to `logs/paper_book.jsonl`
- Dry-run only — no live orders

### 4. Signals (side feature, not the strategy)

```bash
python -m tradingbot summary --top 8
python -m tradingbot summary --symbol HYPE
python -m tradingbot signals --top 10
python -m tradingbot signals --symbol HYPE
```

`summary` is the fast table. Live tickers, cached daily bars. `signals` also runs the slow breakout book.

Default pulls fresh OKX public candles first, then uses the last closed daily bar. `--no-update` is cache only. No orders. Strategy has no take-profit. `TP_2R` is a label only. Empty book means no buy.

### 5. WebSocket smoke test

```bash
python -m tradingbot ws-smoke --symbol BTC-USDT --bar 1H --timeout 12
```

- Connects to OKX public WS, prints channel/instId/rows
- Public market data only

### 5. 24h paper loop (background)

```bash
# Start
python -m tradingbot paper-start --strategies d1 --run-id my_paper --poll 60

# Check status
python -m tradingbot paper-status --run-id my_paper

# Stop
python -m tradingbot paper-stop --run-id my_paper
```

- Runs in background via pidfile
- Paper/dry-run only, never live
- For foreground loop (Ctrl+C to stop): `python -m tradingbot paper-loop --strategies d1 --run-id my_paper --poll 60`

## Safety Rules (hard)

1. **Spot long / USDT only.** No shorts, no futures, no leverage.
2. **No live orders.** `mode: dry_run` default. Live always refused (`OrderManager.submit_live` raises `NotImplementedError`). Even `--live` + `LIVE_APPROVED=1` still refuses.
3. **Never use OKX main balance.** The 6,788 USDT wedding-funds Simple Earn figure is off-limits. `allocated_capital` is a dedicated sub-account notional from `config.yaml` only.
4. **No API keys needed** for dry-run/paper. `.env` has empty `OKX_*` fields — leave them empty.
5. **Public market data only.** OKX REST `/market/*` and public WebSocket. No private endpoints.
6. **Closed candles only** (`confirm == 1`) for signals and sizing.
7. **Do not modify locked v3 files.** Files under `tradingbot/eval/v3_*` and `tradingbot/strategies/spec_v*` are frozen — changing them triggers `FreezeMismatch`.

## Where outputs go

| Path | Contents |
|------|----------|
| `data/cache/*.parquet` | Candle cache from OKX |
| `data/quality_report.json` | Data quality report (after data-sync) |
| `data/quality_report.md` | Human-readable quality report |
| `state/portfolio.json` | Paper portfolio state |
| `logs/tradingbot.jsonl` | Bot event log |
| `logs/paper_book.jsonl` | Paper trade journal (type=init/day) |
| `logs/paper_runner.log` | Paper runner log (if paper-start used) |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `python3: command not found` | Windows uses `python` or `py`, not `python3`. Use `.venv\Scripts\python` or activate venv then `python` |
| `No module named numpy/pandas/requests` | Activate venv: `source .venv/Scripts/activate`, then `pip install -e .` |
| `No BTC-USDT 4H candles available` | Run `data-sync` first to populate `data/cache/`, or use `--no-update` after cache exists |
| `Config error: allocated_capital is required` | Set `allocated_capital` and `paper_notional_usdt` in `config.yaml` (both must match, positive) |
| `FileNotFoundError: '/workspace/...'` | You're on Windows; config now uses relative paths. Re-check `config.yaml` wasn't reverted |
| Backtest is slow | It's O(n²) with all cached bars. Use `--days 60` or `--no-update` with a smaller cache |
| Test `test_paper_runner::test_alive_pid_and_stale_and_stop` fails | `os.kill(pid,0)` Windows quirk — patched in `paper_runner.py`, but some tests still expect Linux behavior |
| Tests referencing `/workspace/...` fail | Those tests run on the Linux build machine, not Windows. Expected to fail locally |

## Developing a New Strategy

1. Create branch: `git switch -c andrew/my-idea`
2. Copy `tradingbot/strategy/placeholder.py` to `tradingbot/strategy/my_idea.py`, rename class + `name`
3. Edit `on_candles(inst_id, candles, position)` — returns `Signal(side="buy"|"close", ...)` or `None`
4. Add `"my_idea"` to valid strategy names in `tradingbot/cli.py::cmd_backtest`
5. Write a test in `tests/` checking no lookahead bias
6. Run: `python -m pytest` then `python -m tradingbot backtest --strategies placeholder,my_idea --days 60`
7. Compare Pooled OOS and 2x-fee stress vs placeholder

## Do Not Touch (locked)

- `tradingbot/eval/v3_*` — v3 engine, protocol, metrics, hypotheses, funding, load, paper
- `tradingbot/strategies/spec_v1_*`, `spec_v2_*` — frozen strategy specs
- `tradingbot/config.py` defaults for paths — already patched for Windows, don't revert to `/workspace/`
