#!/usr/bin/env bash
# ==============================================================================
# RanchoTrade - 24/7 Mobile Launcher for Termux (Android)
#
# Usage in Termux:
#   chmod +x start_termux.sh
#   ./start_termux.sh
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================="
echo "  🚀 Starting RanchoTrade Trading Engine in Termux...  "
echo "======================================================="

# 1. Acquire wake lock so Android OS does not put Python to sleep
if command -v termux-wake-lock >/dev/null 2>&1; then
    echo "[+] Acquiring Termux Wake Lock (prevents background sleep)..."
    termux-wake-lock
fi

# 2. Check Python installation
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[-] ERROR: Python is not installed in Termux!"
    echo "    Run: pkg update && pkg install python -y"
    exit 1
fi

mkdir -p data logs reports

# 3. Cleanup on exit (Ctrl-C)
cleanup() {
    echo ""
    echo "[!] Stopping all background services..."
    kill $(jobs -p) 2>/dev/null || true
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock
    fi
    echo "[+] Stopped cleanly."
}
trap cleanup EXIT INT TERM

# 4. Start 1-minute Indian Market Agent Loop (09:15 - 15:15 IST)
echo "[+] Starting Indian Market Auto-Trader Loop (Mon-Fri 09:15-15:15 IST)..."
(
    while true; do
        # Format current IST time (HHMM)
        CURRENT_TIME=$(TZ="Asia/Kolkata" date +%H%M)
        DAY_OF_WEEK=$(TZ="Asia/Kolkata" date +%u) # 1=Mon, 5=Fri, 6=Sat, 7=Sun

        # Only evaluate during weekday market hours (09:15 - 15:15 IST)
        if [ "$DAY_OF_WEEK" -le 5 ] && [ "$CURRENT_TIME" -ge 0915 ] && [ "$CURRENT_TIME" -le 1515 ]; then
            echo "[$(TZ="Asia/Kolkata" date '+%Y-%m-%d %H:%M:%S')] Running mobile agent pass..." >> logs/trader_schedule.log
            "$PY" -m trading.agents.agent_trader --with-scanner --max-trades 3 >> logs/trader_schedule.log 2>&1 || true
        fi
        sleep 60
    done
) &

# 5. Start RanchoTrade Dashboard (includes 24/7 Crypto/Forex Desk on Delta Exchange)
echo "[+] Starting RanchoTrade Live Server on port 8787..."
echo ""
echo "======================================================="
echo "  🌐 Dashboard URL: http://localhost:8787"
echo "  📱 Open Chrome / Brave on your phone & navigate to:"
echo "     http://localhost:8787"
echo "======================================================="
echo ""

# Launch browser if termux-open-url is available
if command -v termux-open-url >/dev/null 2>&1; then
    (sleep 2 && termux-open-url "http://localhost:8787") &
fi

exec "$PY" -u -m trading.dashboard 8787
