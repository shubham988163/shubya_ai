#!/usr/bin/env bash
# TradingView bridge launcher — starts the webhook receiver + a free
# Cloudflare quick-tunnel, and prints everything you need to paste into
# TradingView (secret + webhook URL).
#
# Usage:  ./scripts/tv_bridge.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data

if ! command -v cloudflared >/dev/null; then
  echo "ERROR: cloudflared not installed (macOS: brew install cloudflared)"
  exit 1
fi

PORT=8788
SECRET_FILE="data/tv_secret"

# persistent secret (generated once; must match the Pine input)
if [ ! -f "$SECRET_FILE" ]; then
  openssl rand -hex 16 > "$SECRET_FILE"
  echo "generated new webhook secret -> $SECRET_FILE"
fi
export TV_WEBHOOK_SECRET="$(cat "$SECRET_FILE")"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT

.venv/bin/python -m trading.tv_webhook "$PORT" &

TUNNEL_LOG="$(mktemp)"
cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate >"$TUNNEL_LOG" 2>&1 &

echo "waiting for tunnel..."
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

echo
echo "==============================================================="
echo " TradingView alert settings"
echo "   Webhook URL : ${URL:-<tunnel failed — check $TUNNEL_LOG>}/webhook"
echo "   Message     : {{strategy.order.alert_message}}"
echo "   Condition   : your strategy -> 'Order fills only'"
echo
echo " Pine strategy input"
echo "   Webhook secret : $TV_WEBHOOK_SECRET"
echo "==============================================================="
echo
echo "NOTE: quick-tunnel URLs change on every restart — update the alert's"
echo "webhook URL when you relaunch. Ctrl-C stops receiver + tunnel."
wait
