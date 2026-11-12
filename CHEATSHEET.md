# Command cheat sheet

Paper only. No OKX keys. No live orders.

Two sections: Windows first, Mac second. Same bot commands after the venv is on. Do not copy the Windows `.venv` to the Mac. Make a new one there.

## Windows

Git Bash.

Every new window, run this first:

```bash
cd /c/Users/ADMIN/Documents/GitHub/okx-spot-trading-bot
source .venv/Scripts/activate
export PYTHONUTF8=1
```

## 1. First time, or repair a broken venv

Do not delete `.venv` if it already exists. This block is the one that works:

```bash
cd /c/Users/ADMIN/Documents/GitHub/okx-spot-trading-bot
source .venv/Scripts/activate
export PYTHONUTF8=1
python -m pip install -U pip
pip install -e .
python -c "import numpy, pandas, requests, pyarrow; print('ok')"
```

Only if `.venv` does not exist yet:

```bash
py -3.14 -m venv .venv
source .venv/Scripts/activate
export PYTHONUTF8=1
python -m pip install -U pip
pip install -e .
cp -n .env.example .env
python -c "import numpy, pandas, requests, pyarrow; print('ok')"
```

`ok` means the install worked. If `pip install -e .` mentions multiple top-level packages, pull the latest `pyproject.toml` and run the repair block again. Do not keep going until `ok` prints.

## 2. Pull real OKX data

```bash
python -m tradingbot data-sync --bars 4H,1Dutc
```

Cache only, no download:

```bash
python -m tradingbot data-sync --bars 1Dutc --no-update
```

## 3. Print tables

Top 10 closest eligible coins, live prices:

```bash
python -m tradingbot summary --top 10
```

One coin, example HYPE:

```bash
python -m tradingbot summary --symbol HYPE
```

Both in one go:

```bash
python -m tradingbot summary --top 10 --symbol HYPE
```

Slow full book, with stops. Still no strategy take-profit. `TP_2R` is a label only. Use `--no-update` so it does not re-download.

```bash
python -m tradingbot signals --no-update --top 10
python -m tradingbot signals --no-update --symbol HYPE
```

## 4. Paper, one day, no real order

```bash
python -m tradingbot paper-once --strategy my_d1 --no-update
python -m tradingbot paper-once --strategy d1 --no-update
python -m tradingbot paper-status
```

Leave a window open:

```bash
python -m tradingbot paper-loop --strategies my_d1 --run-id andrew_local --poll 60
```

Background start / stop:

```bash
python -m tradingbot paper-start --strategies my_d1 --run-id andrew_local --poll 60
python -m tradingbot paper-status --run-id andrew_local
python -m tradingbot paper-stop --run-id andrew_local
```

## 5. Backtest

Max 3 names. `my_d1` needs `--bar 1Dutc`. The second command is slow.

```bash
python -m tradingbot backtest --no-update --strategies placeholder --bar 4H --days 60
python -m tradingbot backtest --no-update --strategies my_d1 --bar 1Dutc --days 400
```

Read the `Pooled OOS` line. Ignore a pretty full-window number if OOS is bad.

## 6. Tune

Train picks the size. Holdout is the score. Cash is always allowed. These take several minutes. Run one at a time. No extra flags.

```bash
python scripts/classic_screen.py
python scripts/d1_param_sweep.py --days 600
python scripts/risk_constrained_opt.py
```

Current unlocked book: lookback 20, half size. File: `tradingbot/strategy/my_d1.py`.

## 7. New strategy

Do this on a new branch. Do not edit locked files.

Locked, do not touch:

- `tradingbot/strategy/d1_breakout.py`
- `tradingbot/eval/v3_*.py`
- `tradingbot/strategies/spec_v1_*.py`
- `tradingbot/strategies/spec_v2_*.py`

```bash
git switch -c andrew/my-idea
cp tradingbot/strategy/placeholder.py tradingbot/strategy/my_idea.py
```

Then:

1. In `my_idea.py`, rename the class and `name`. Edit `on_candles`. Return a buy, a close, or nothing.
2. In `tradingbot/cli.py`, add `"my_idea"` to the allowed backtest names.
3. Add a block that builds your class, next to the `placeholder` block in `cmd_backtest`.
4. Copy one test in `tests/` that checks the strategy does not see the future.
5. Run:

```bash
python -m pytest -q
python -m tradingbot backtest --no-update --strategies placeholder --bar 4H --days 60
```

On Windows, some tests fail because they look for `/workspace/...`. That is the Linux build machine, not a bad install. `summary` printing a table means the bot works.

`paper-once` will not know the new name until you also add it to the `--strategy` choices and to `run_paper_iteration`.

## 8. What the table words mean

| Word | Meaning |
|------|---------|
| Gate ON | BTC last close is above its 200-day average |
| Breadth | Share of the liquidity top-20 above their 50-day average. New buys want 50% or more |
| Gap | How far the live price is under the prior 20-day high |
| Valid long SL | 20-day mid is below the live price. If it is above, there is no long stop |
| Pooled OOS | The part the rules did not peek at. Trust this more than the full window |
| PF | Profit divided by loss. Under 1 means losses were bigger |

## 9. Do not

- Do not put OKX keys in `.env` for paper.
- Do not use the 6,788 USDT wallet as capital.
- `--live` still refuses. No real orders.

## Mac

Terminal, not Git Bash. Python 3.11 or newer. `python3` if `python` is missing.

Clone or copy the repo, then `cd` into that folder. Example if you put it in Documents:

```bash
cd ~/Documents/GitHub/okx-spot-trading-bot
```

Every new Terminal window:

```bash
cd ~/Documents/GitHub/okx-spot-trading-bot
source .venv/bin/activate
```

First time only. Delete a copied Windows `.venv` if you brought one over.

```bash
cd ~/Documents/GitHub/okx-spot-trading-bot
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e .
cp -n .env.example .env
python -c "import numpy, pandas, requests, pyarrow; print('ok')"
```

Stop if that line does not print `ok`.

If `python3 -m venv` fails, install Python 3 from python.org, then run that block again.

Pull real OKX data:

```bash
python -m tradingbot data-sync --bars 4H,1Dutc
```

Top 10 table:

```bash
python -m tradingbot summary --top 10
```

One coin:

```bash
python -m tradingbot summary --symbol HYPE
```

Both:

```bash
python -m tradingbot summary --top 10 --symbol HYPE
```

Slow book, with stops. `TP_2R` is a label only. `--no-update` uses the cache.

```bash
python -m tradingbot signals --no-update --top 10
python -m tradingbot signals --no-update --symbol HYPE
```

Paper, no real order:

```bash
python -m tradingbot paper-once --strategy my_d1 --no-update
python -m tradingbot paper-status
```

Leave the window open:

```bash
python -m tradingbot paper-loop --strategies my_d1 --run-id andrew_local --poll 60
```

Background start / stop:

```bash
python -m tradingbot paper-start --strategies my_d1 --run-id andrew_local --poll 60
python -m tradingbot paper-status --run-id andrew_local
python -m tradingbot paper-stop --run-id andrew_local
```

Backtest:

```bash
python -m tradingbot backtest --no-update --strategies placeholder --bar 4H --days 60
python -m tradingbot backtest --no-update --strategies my_d1 --bar 1Dutc --days 400
```

Tune. Several minutes. One at a time.

```bash
python scripts/classic_screen.py
python scripts/d1_param_sweep.py --days 600
python scripts/risk_constrained_opt.py
```

New strategy:

```bash
git switch -c andrew/my-idea
cp tradingbot/strategy/placeholder.py tradingbot/strategy/my_idea.py
```

Wire `my_idea` into `tradingbot/cli.py` before the backtest line. Until then, this is the command that already works:

```bash
python -m tradingbot backtest --no-update --strategies placeholder --bar 4H --days 60
```

Same locked-file rules as Windows. Do not edit `d1_breakout.py` or the `v3_` / `spec_v1_` / `spec_v2_` files.

Mac notes:

- Activate with `.venv/bin/activate`, not `.venv/Scripts/activate`.
- No `py -3.14`. Use `python3 -m venv .venv`.
- No `export PYTHONUTF8=1` needed.
- `data/cache/` is not in git. Run `data-sync` on the Mac. Do not expect the Windows cache to be there unless you copied it.
