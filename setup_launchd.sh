#!/bin/bash
# Installs (or reinstalls) the daily launchd job that runs run_daily.py.
# Safe to re-run any time you edit the .plist (e.g. to change the schedule).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.salihyilmaz.bist-magic-formula.plist"
SRC="$REPO_DIR/$PLIST_NAME"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_DIR/logs" "$REPO_DIR/archive"

# Unload the previous version first (no-op, and quiet, if not currently loaded)
launchctl unload "$DEST" 2>/dev/null || true

cp "$SRC" "$DEST"
launchctl load -w "$DEST"

echo "Installed and loaded: $DEST"
echo "Scheduled to run daily at 19:00 local time."
echo
echo "Useful commands:"
echo "  launchctl list | grep bist-magic-formula     # confirm it's loaded"
echo "  launchctl start com.salihyilmaz.bist-magic-formula   # trigger a run right now"
echo "  launchctl unload $DEST                        # stop/disable the daily run"
echo "  tail -f $REPO_DIR/logs/run_\$(date +%Y%m%d).log   # watch today's run"
