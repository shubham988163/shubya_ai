#!/usr/bin/env bash
# Full automated trading day — designed to be launched by cron at 9:05 IST
# on weekdays. Runs: pre-market agent -> supervisor + engine (engine exits
# itself after market close) -> EOD journal (only if there were trades).
#
# Remove the automation any time with:  crontab -e  (delete the ai-tradeagent line)
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs reports

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "ERROR: no virtualenv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# cron does not read ~/.zshrc — pull just the API key export from it
eval "$(grep '^export GEMINI_API_KEY' ~/.zshrc 2>/dev/null)" || true
export PYTHONUNBUFFERED=1

echo "=== $(date '+%F %T') automated day start ==="
$PY -m trading.agents.premarket

$PY -m trading.agents.supervisor --loop &
SUPERVISOR_PID=$!

$PY -m trading.strategy            # blocks until market close, then exits

kill "$SUPERVISOR_PID" 2>/dev/null || true

TRADES_TODAY=$(sqlite3 data/ledger.db "SELECT COUNT(*) FROM trades WHERE date=date('now','localtime');")
if [ "${TRADES_TODAY:-0}" != "0" ]; then
  $PY -m trading.agents.eod_journal
else
  echo "no trades today — skipping journal"
fi
echo "=== $(date '+%F %T') automated day end ==="
