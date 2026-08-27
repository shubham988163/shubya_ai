#!/usr/bin/env bash
# Full automated trading day — designed to be launched by cron at 9:05 IST
# on weekdays. Runs: pre-market agent -> supervisor + engine (engine exits
# itself after market close) -> EOD journal (only if there were trades).
#
# Remove the automation any time with:  crontab -e  (delete the ai-tradeagent line)
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs reports

# launchd (unlike cron) runs a missed job when the machine wakes, which is the
# whole point of the switch — but it means this can fire at any hour. Bail out
# rather than burn LLM quota on a pre-market analysis for a session that is
# already over, or overwrite today_config.json with a useless late run.
DOW=$(TZ=Asia/Kolkata date '+%u')          # 1=Mon .. 7=Sun
NOW=$(TZ=Asia/Kolkata date '+%H%M')
if [ "$DOW" -gt 5 ]; then
  echo "=== $(TZ=Asia/Kolkata date '+%F %T') weekend (dow=$DOW) — skipping ==="
  exit 0
fi
if [ "$((10#$NOW))" -ge 1530 ]; then
  echo "=== $(TZ=Asia/Kolkata date '+%F %T') market already closed — skipping ==="
  exit 0
fi

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

# The engine blocks until market close, then exits 0. Exit 75 means it went
# blind on market data and wants a fresh process (see MAX_BLIND_SCANS in
# strategy.py) — on 2026-08-10 a yfinance fd leak blacked it out for ~40
# minutes and it had no way to recover in-process. Restart it, bounded, and
# never past market close.
MAX_RESTARTS=6
restarts=0
while :; do
  $PY -m trading.strategy
  rc=$?
  [ "$rc" -ne 75 ] && break

  now=$(TZ=Asia/Kolkata date '+%H%M')
  if [ "$((10#$now))" -ge 1530 ]; then
    echo "data blackout at $now IST but market is closed — not restarting"
    break
  fi
  restarts=$((restarts + 1))
  if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
    echo "ERROR: engine hit $restarts data blackouts — giving up for today."
    echo "       open positions are still in the ledger; square off manually."
    break
  fi
  echo "=== $(date '+%F %T') engine blackout (rc=$rc) — restart $restarts/$MAX_RESTARTS ==="
  sleep 15
done

kill "$SUPERVISOR_PID" 2>/dev/null || true

TRADES_TODAY=$(sqlite3 data/ledger.db "SELECT COUNT(*) FROM trades WHERE date=date('now','localtime');")
if [ "${TRADES_TODAY:-0}" != "0" ]; then
  $PY -m trading.agents.eod_journal
else
  echo "no trades today — skipping journal"
fi
echo "=== $(date '+%F %T') automated day end ==="
