#!/usr/bin/env bash
# One-command trading day: dashboard + supervisor agent + TradingView bridge.
#
#   ./scripts/start_day.sh
#
# Ctrl-C stops everything. Requires GEMINI_API_KEY in the environment
# (put `export GEMINI_API_KEY=...` in ~/.zshrc) — without it the agents
# run in safe-fallback mode.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs reports

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "ERROR: no virtualenv found. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
export PYTHONUNBUFFERED=1   # stream child output live into logs

if [ -z "${GEMINI_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARNING: no GEMINI_API_KEY / ANTHROPIC_API_KEY — agents will use fallbacks."
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT

# 1. Dashboard
$PY -m trading.dashboard 8787 &
echo "dashboard    : http://localhost:8787"

# 2. Supervisor agent — reviews every new trade in the ledger
$PY -m trading.agents.supervisor --loop &
echo "supervisor   : polling ledger"

# 3. Python strategy engine — EMA 9/21 on the core watchlist (Jul-7 setup)
$PY -m trading.strategy &
echo "strategy     : EMA 9/21 on core watchlist (live loop)"

# 3. TradingView bridge (receiver + tunnel) — prints webhook URL + secret
exec ./scripts/tv_bridge.sh
