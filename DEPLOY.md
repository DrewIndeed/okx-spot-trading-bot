# Deploy guide — run the bot somewhere + get Telegram notifications

Bot status: **paper / dry-run only**. It never holds OKX API keys, never places live
orders, never touches the 6,788 USDT main balance. Deployment = running the paper loop
somewhere + pushing alert messages to your phone.

## 1. Where to deploy — pick one

| Option                                                    | Cost | Verdict       | Why                                                                                                                                            |
| --------------------------------------------------------- | ---- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| **GitHub Actions cron (loop mode)**                       | $0   | **Best free** | Runs `paper-once` on schedule, commits paper state back to the repo, sends Telegram. Public repo = free unlimited minutes for standard runners |
| **Oracle Cloud Always Free** (ARM Ampere A1, 4 vCPU/24GB) | $0   | Best free VPS | Real server, systemd, persistent disk. Signup needs card for ID check (never charged on Always Free)                                           |
| **Vercel / Netlify / Render / Railway**                   | —    | ❌            | Serverless no disk / trial expires / paid. Wrong tool                                                                                          |

**Recommendation (free-only): GitHub Actions loop.** Zero server. State is committed back
to the repo between runs, so the paper book persists. Oracle VPS is the upgrade path if
you later want a real 24/7 process without GitHub cadence limits.

> **Pick ONE place that owns the paper book** (`state/portfolio.json`): the GitHub
> Actions loop (1a), the systemd `okxbot` service (section 4), or `/startloop` from
> Telegram. Running two of them at once means two writers on the same state.
> GitHub only fires `schedule:` triggers from the **default branch**, so the Actions
> loop does nothing until the workflow file is merged to `main`.
> The bot does not read `.env` itself: systemd loads it via `EnvironmentFile`; on your
> own PC export `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the shell first.

## 1a. FREE path A — GitHub Actions paper loop (no server at all)

Requirements: repo on GitHub (public = free unlimited runner minutes; private = 2000
min/month, likely too little for 30-min cadence — use public or hourly cron).

1. Add repo secrets (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN` — from @BotFather (see section 2)
   - `TELEGRAM_CHAT_ID` — your chat id (see section 2)
2. Commit `.github/workflows/paper-loop.yml`:

```yaml
name: paper-loop
on:
  schedule:
    - cron: "*/30 * * * *" # every 30 min; use "0 * * * *" for hourly
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: paper-loop
  cancel-in-progress: false

jobs:
  once:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.ref_name }}
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - name: Data sync (1Dutc only, keeps run fast)
        run: python -m tradingbot data-sync --bars 1Dutc --days 400
      - name: One paper iteration (sends Telegram equity alert)
        run: python -m tradingbot paper-once --strategy my_d1
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - name: Commit paper state
        run: |
          git config user.name  "paper-bot"
          git config user.email "paper-bot@users.noreply.github.com"
          # state/ and logs/ are gitignored -> force-add, they ARE the persistence
          git add -f state/portfolio.json logs/paper_book.jsonl 2>/dev/null || true
          git diff --cached --quiet || git commit -m "paper state update [skip ci]"
          git pull --rebase origin || true
          git push
```

3. Run it once by hand: Actions tab → paper-loop → Run workflow. Then it self-schedules.

How it works / limits:

- `paper-once` reads `state/portfolio.json` from the repo, updates equity/positions on the
  last closed daily bar, writes it back. That IS the persistence — no server disk needed.
- Every iteration calls `send_alert(...)` → Telegram message each run. Noise? Switch cron
  to `"0 * * * *"` (hourly) or `"5 0 * * *"` (daily).
- Each run re-pulls ~125 symbols of daily candles (~3-4 min). Runner-free on public repos.
- `data/cache` parquet is NOT committed (gitignored, too big) — pulled fresh each run.
- Nothing here can place live orders: no OKX keys exist anywhere in this setup.

## 1b. FREE path B — Oracle Cloud Always Free VPS (real server, $0)

1. Sign up: oracle.com/cloud/free → "Start for free". Card needed for identity check
   (Always Free resources are never charged). Pick home region with ARM capacity.
2. Create instance: Compute → Instances → Create. Image: **Ubuntu 22.04/24.04**,
   Shape: **Ampere A1 Flex** → 2-4 OCPU, 12-24 GB (all inside Always Free). Save the
   SSH key you provide.
3. SSH in: `ssh -i <key> ubuntu@<public-ip>`
4. Continue with **section 3** below (clone, venv, `.env`, data-sync, notify-test),
   then **section 4** (systemd service). Same steps, free machine.

## 2. One-time: create the Telegram bot (10 min)

1. In Telegram, open **@BotFather** → `/newbot` → pick a name → you get a token like
   `123456:ABC-xxxx`. That is `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and press **Start** (send `/start`). Bots cannot
   message you first.
3. Get your chat id: open in browser
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → find `"chat":{"id": 123456789}`.
   That number is `TELEGRAM_CHAT_ID`. (For a group: add the bot to the group, id is
   negative like `-100123...`.)

## 3. VPS deploy (Ubuntu 22/24)

```bash
# on the VPS
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/<you>/okx-spot-trading-bot.git
cd okx-spot-trading-bot
python3 -m venv .venv
.venv/bin/pip install -e .

# .env — secrets, never commit
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_CHAT_ID=<your chat id>
EOF
chmod 600 .env

# pull real OKX data (public REST, no keys)
.venv/bin/python -m tradingbot data-sync --bars 1Dutc

# test Telegram end-to-end
set -a; source .env; set +a
.venv/bin/python -m tradingbot notify-test     # -> "Telegram sent"

# one paper iteration to sanity check
.venv/bin/python -m tradingbot paper-once --strategy my_d1
```

## 4. Keep it running: systemd service

```bash
sudo tee /etc/systemd/system/okxbot.service >/dev/null <<'EOF'
[Unit]
Description=OKX spot paper bot (my_d1, paper only)
After=network-online.target

[Service]
Type=simple
User=<your vps user>
WorkingDirectory=/home/<your vps user>/okx-spot-trading-bot
EnvironmentFile=/home/<your vps user>/okx-spot-trading-bot/.env
ExecStart=/home/<your vps user>/okx-spot-trading-bot/.venv/bin/python -m tradingbot paper-loop --strategies my_d1 --poll 3600
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now okxbot
sudo systemctl status okxbot          # should say active (running)
journalctl -u okxbot -f               # live logs
```

`--poll 3600` = one cycle per hour (daily bars, hourly check is plenty).
Each cycle it already calls `send_alert(...)` → **Telegram message with equity**.
You get a message every hour. Want less noise? `--poll 86400` (daily) or lower it.

Stop / update:

```bash
sudo systemctl stop okxbot                        # stop
cd okx-spot-trading-bot && git pull && .venv/bin/pip install -e . && sudo systemctl restart okxbot
```

## 5. Telegram command bot (chat control of every step)

Long polling needs an always-on process. Free: **Oracle Always Free VPS** (path B)
or your own PC. NOT GitHub Actions — serverless cron can't hold a polling process.

Setup (same `.env` as before):

```bash
sudo tee /etc/systemd/system/okxbot-tg.service >/dev/null <<'EOF'
[Unit]
Description=OKX paper bot Telegram command interface
After=network-online.target

[Service]
Type=simple
User=<your vps user>
WorkingDirectory=/home/<your vps user>/okx-spot-trading-bot
EnvironmentFile=/home/<your vps user>/okx-spot-trading-bot/.env
ExecStart=/home/<your vps user>/okx-spot-trading-bot/.venv/bin/python -m tradingbot tg-bot
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now okxbot-tg
journalctl -u okxbot-tg -f
```

Then in Telegram you get a command menu (auto-registered via setMyCommands):

| Command      | Runs                        | Speed           |
| ------------ | --------------------------- | --------------- |
| `/status`    | paper-status                | instant         |
| `/summary`   | summary --top 8             | instant         |
| `/signals`   | signals --no-update --top 5 | instant         |
| `/notify`    | notify-test                 | instant         |
| `/sync`      | data-sync --bars 1Dutc      | ~2-4 min        |
| `/backtest`  | backtest my_d1, 400d        | ~1 min          |
| `/once`      | paper-once my_d1            | seconds-minutes |
| `/startloop` | paper-start my_d1, hourly   | seconds         |
| `/stoploop`  | paper-stop                  | seconds         |
| `/help`      | menu                        | instant         |

`/start` is Telegram's built-in "open chat" command: it only shows the welcome message.

Safety: only the chat id(s) in `TELEGRAM_CHAT_ID` get answers; other chats are
ignored silently. Unknown commands get the menu. Long commands queue one at a
time. No live trading exists in the codebase to expose.

## 6. Daily signal digest (optional, second unit)

```bash
sudo tee /etc/systemd/system/okxbot-signals.timer >/dev/null <<'EOF'
[Unit]
Description=Daily OKX signals digest

[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/okxbot-signals.service >/dev/null <<'EOF'
[Unit]
Description=OKX signals digest -> Telegram
After=network-online.target

[Service]
Type=oneshot
User=<your vps user>
WorkingDirectory=/home/<your vps user>/okx-spot-trading-bot
EnvironmentFile=/home/<your vps user>/okx-spot-trading-bot/.env
ExecStart=/bin/bash -c '.venv/bin/python -m tradingbot signals --top 5 | tail -40 | .venv/bin/python -c "import sys;from tradingbot.alerts import send_alert;send_alert(sys.stdin.read())"'
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now okxbot-signals.timer
```

## 6b. GitHub Actions cron (free alternative, notify-only)

`.github/workflows/signals.yml`:

```yaml
name: signals-digest
on:
  schedule:
    - cron: "5 0 * * *"   # 00:05 UTC daily
  workflow_dispatch: {}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - run: |
          python -m tradingbot data-sync --bars 1Dutc --days 400
        env: {}
      - run: |
          PYTHONUTF8=1 python -m tradingbot signals --top 5 | tail -40 | python -c "
import sys
from tradingbot.alerts import send_alert
send_alert(sys.stdin.read())
"
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

Add the two repo secrets under Settings → Secrets → Actions. Note: each run re-pulls
~125 symbols of daily candles (~2-4 min), no state between runs — good for digests,
wrong for the equity loop.

## 7. WhatsApp?

Possible but heavier: WhatsApp Business Cloud API needs a Meta developer app +
business verification, or Twilio (paid, per message). Telegram is free, 2-minute
setup, and the code already speaks it. **Use Telegram.** If you must have WhatsApp
later, the swap point is one function: `tradingbot/alerts.py::send_alert` — replace
the Telegram POST with your Twilio/Meta call, everything else unchanged.

## 8. Safety recap (unchanged by deployment)

- No OKX API keys anywhere in the bot or `.env`. Public REST + public WS only.
- `paper-loop` / `paper-once` write to local `state/` files — sim money only.
- Live orders are refused in code (`ensure_safe_mode`). Deployment does not change that.
- `.env` is gitignored. Never paste the bot token into chat, logs, or code.
