"""Telegram command bot — chat commands run the paper bot's CLI steps.

Security rules:
- Only answers chat ids listed in TELEGRAM_CHAT_ID (comma separated). Others ignored.
- Command allow-list only: sync, summary, signals, backtest, once, startloop,
  stoploop, status, notify, help. No live trading exists anywhere to expose.
- Long commands run one at a time in a worker thread; quick ones answer inline.
- Output is sent as monospace chunks (Telegram cap 4096 chars).

Note: /start is Telegram's built-in "open chat" command and only shows the
welcome message. The paper loop is controlled with /startloop and /stoploop.

Run: python -m tradingbot tg-bot      (needs TELEGRAM_BOT_TOKEN in env)
Deploy: keep-alive process (systemd on a free VPS, or your PC). NOT for
GitHub Actions cron — polling needs a persistent process.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Optional

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
QUICK_TIMEOUT = 120  # seconds for inline commands
LONG_TIMEOUT = 900  # seconds for sync/backtest/once/startloop

# name -> (cli args, is_long, description)
COMMANDS: dict[str, tuple[list[str], bool, str]] = {
    "status":    (["paper-status"], False, "is the paper loop running?"),
    "summary":   (["summary", "--top", "8"], False, "live market + gate summary"),
    "signals":   (["signals", "--no-update", "--top", "5"], False, "closest breakout setups (cache)"),
    "notify":    (["notify-test"], False, "send a Telegram test message"),
    "stoploop":  (["paper-stop"], False, "stop the background paper loop"),
    "help":      ([], False, "list commands"),
    "sync":      (["data-sync", "--bars", "1Dutc"], True, "refresh OKX daily cache (~2-4 min)"),
    "backtest":  (["backtest", "--no-update", "--strategies", "my_d1", "--bar", "1Dutc", "--days", "400"], True, "run my_d1 backtest (~1 min)"),
    "once":      (["paper-once", "--strategy", "my_d1"], True, "one paper iteration now"),
    "startloop": (["paper-start", "--strategies", "my_d1", "--poll", "3600"], True, "start background paper loop"),
}

MENU = "\n".join(f"/{n} - {d}" for n, (_a, _l, d) in COMMANDS.items())


def _api(token: str, method: str, payload: Optional[dict] = None, timeout: int = 35):
    url = API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send(token: str, chat: str | int, text: str) -> None:
    body = html.escape(text)
    while body:
        chunk, body = body[:3500], body[3500:]
        _api(token, "sendMessage", {
            "chat_id": chat,
            "text": f"<pre>{chunk}</pre>",
            "parse_mode": "HTML",
        })


def _cli(args: list[str], timeout: int) -> str:
    """Run a tradingbot CLI step and return its output.

    stdout/stderr go to temp files instead of pipes: if a command (e.g.
    paper-start) leaves a detached child holding the pipe open, a pipe-based
    capture would block until the timeout even though the command returned.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.run(
            [sys.executable, "-m", "tradingbot", *args],
            stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f,
            timeout=timeout, env=env,
        )
        out_f.seek(0)
        err_f.seek(0)
        out = out_f.read().decode("utf-8", errors="replace").rstrip()
        err = err_f.read().decode("utf-8", errors="replace").strip()
    if err:
        out += ("\n[stderr]\n" + err)
    return out or f"(no output, exit {proc.returncode})"


def _safe_cli(args: list[str], timeout: int) -> str:
    """Like _cli, but always returns text (timeouts/errors become messages)."""
    try:
        return _cli(args, timeout)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except Exception as e:
        return f"failed: {e}"


def _handle(token: str, chat: str, text: str, lock: threading.Lock) -> None:
    """Handle one message. Sends any reply itself."""
    raw = text.strip().split(maxsplit=1)
    if not raw or not raw[0].startswith("/"):
        return
    name = raw[0][1:].split("@")[0].lower()  # strip "/cmd@BotName"
    entry = COMMANDS.get(name)
    if entry is None:
        _send(token, chat, f"Unknown command /{name}\n{MENU}")
        return
    args, is_long, _desc = entry
    if name == "help":
        _send(token, chat, "okx paper bot commands\n" + MENU +
              "\n\nPaper / dry-run only. No OKX keys. No live orders.")
        return
    if not is_long:
        _send(token, chat, _safe_cli(args, QUICK_TIMEOUT))
        return
    # long commands: one at a time, announce then report
    if not lock.acquire(blocking=False):
        _send(token, chat, f"busy. /{name} not started: another long command is still running. Try again when it finishes.")
        return
    try:
        _send(token, chat, f"running /{name} ... will report when done.")
    except Exception:
        lock.release()
        raise

    def job() -> None:
        try:
            _send(token, chat, _safe_cli(args, LONG_TIMEOUT))
        except Exception as e:
            print(f"/{name} reply failed: {e}", flush=True)
        finally:
            lock.release()

    threading.Thread(target=job, daemon=True).start()


def run_tg_bot() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing. Export it in the environment (this bot does not read .env itself).")
        return 2
    allowed = {c.strip() for c in (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()}
    if not allowed:
        print("TELEGRAM_CHAT_ID missing. The bot refuses to answer unknown chats.")
        return 2

    me = _api(token, "getMe", timeout=15)
    if not me.get("ok"):
        print(f"Telegram getMe failed: {me}")
        return 2
    print(f"tg-bot up as @{me['result']['username']}. Allowed chats: {sorted(allowed)}")
    try:
        _api(token, "setMyCommands", {
            "commands": [
                {"command": n, "description": d}
                for n, (_a, _l, d) in COMMANDS.items() if n != "help"
            ]
        }, timeout=15)
        print("command menu registered with Telegram")
    except Exception as e:
        print(f"setMyCommands failed (non-fatal): {e}")

    lock = threading.Lock()
    offset = 0
    while True:
        try:
            resp = _api(token, "getUpdates", {
                "offset": offset, "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            }, timeout=POLL_TIMEOUT + 10)
            for upd in resp.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if chat_id not in allowed:
                    continue  # silent: unknown chat
                text = msg.get("text") or ""
                try:
                    words = text.split()
                    if words and words[0].split("@")[0].lower() == "/start":
                        _send(token, chat_id, "okx paper bot ready.\n" + MENU)
                        continue
                    _handle(token, chat_id, text, lock)
                except Exception as e:
                    # one bad message must not drop the rest of the batch
                    print(f"tg-bot message error: {e}", flush=True)
        except KeyboardInterrupt:
            print("tg-bot stopped")
            return 0
        except Exception as e:
            print(f"tg-bot loop error: {e}; retry in 10s", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(run_tg_bot())