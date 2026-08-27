#!/usr/bin/env bash
# Install (or remove) the daily trading session as a launchd agent, and retire
# the old crontab entry so the day can never be started twice.
#
#   ./scripts/install_schedule.sh              # install / reinstall
#   ./scripts/install_schedule.sh --uninstall  # remove
#   ./scripts/install_schedule.sh --status     # show what is scheduled
#
# cron skips jobs while the Mac is asleep and never catches up (2026-08-11 and
# 2026-08-13 were both lost that way). launchd runs a missed
# StartCalendarInterval job on the next wake.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.aitradeagent.day"
SRC="$ROOT/scripts/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

unload_agent() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
    || launchctl unload -w "$DEST" 2>/dev/null \
    || true
}

drop_crontab_entry() {
  if crontab -l 2>/dev/null | grep -q "cron_day.sh"; then
    crontab -l 2>/dev/null | grep -v "cron_day.sh" | crontab -
    echo "removed the old crontab entry (launchd now owns the schedule)"
  fi
}

status() {
  echo "--- launchd ---"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl print "$DOMAIN/$LABEL" | grep -E "state|program|runs|last exit" | sed 's/^/  /'
  else
    echo "  $LABEL is NOT loaded"
  fi
  echo "--- crontab ---"
  crontab -l 2>/dev/null | grep "cron_day.sh" || echo "  no cron_day.sh entry (good)"
}

case "${1:-install}" in
  --status) status; exit 0 ;;
  --uninstall)
    unload_agent
    rm -f "$DEST"
    echo "removed $LABEL"
    status
    exit 0
    ;;
esac

[ -f "$SRC" ] || { echo "ERROR: missing $SRC"; exit 1; }
[ -x "$ROOT/scripts/cron_day.sh" ] || chmod +x "$ROOT/scripts/cron_day.sh"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_ROOT__|$ROOT|g" "$SRC" > "$DEST"

if ! plutil -lint "$DEST" >/dev/null; then
  echo "ERROR: generated plist is invalid"; rm -f "$DEST"; exit 1
fi

unload_agent          # idempotent reinstall
if ! launchctl bootstrap "$DOMAIN" "$DEST" 2>/dev/null; then
  launchctl load -w "$DEST" || { echo "ERROR: could not load $LABEL"; exit 1; }
fi

drop_crontab_entry
echo "installed $LABEL -> 09:05 IST, Mon-Fri, catches up after wake"
echo
status
