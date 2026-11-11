#!/usr/bin/env bash
# Supervisor for the *live-ready worktree* paper/demo runner only.
# NEVER manage, signal, restart, or inspect-and-kill pid 129703
# (the official spec_v2 paper runner in /workspace/trading-bot).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OFFICIAL_PID=129703
OFFICIAL_ROOT="/workspace/trading-bot"

if [[ "$ROOT" == "$OFFICIAL_ROOT" || "$ROOT" == "$OFFICIAL_ROOT/"* && "$ROOT" != *"/workspace/trading-bot-live"* ]]; then
  echo "refusing to run from the official trading-bot tree" >&2
  exit 2
fi

if [[ "${1:-}" == "" ]]; then
  set -- "$ROOT/.venv/bin/python" -m okxspot paper --config "$ROOT/engine/config/default.yaml" --demo --once
fi

export PYTHONPATH="${PYTHONPATH:-}:$ROOT/engine/src:$ROOT"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m okxspot supervise \
  --config "${OKXSPOT_CONFIG:-$ROOT/engine/config/default.yaml}" \
  --protect-pid "$OFFICIAL_PID" \
  -- "$@"
