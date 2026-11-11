#!/usr/bin/env bash
# Run both test suites with the repo venv. Reproducible from a clean box after:
#   .venv/bin/pip install -e ".[ ]"  # tradingbot deps already in pyproject
#   .venv/bin/pip install -e "engine[dev]"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; create it and pip install -e \"engine[dev]\" plus tradingbot deps" >&2
  exit 1
fi
.venv/bin/pip install -e "engine[dev]" -q
echo "=== tradingbot ==="
.venv/bin/python -m pytest --override-ini addopts= -q --tb=no
echo "=== engine/okxspot ==="
cd "$ROOT/engine"
PYTHONPATH=src "$ROOT/.venv/bin/python" -m pytest --override-ini addopts=-q --tb=no
