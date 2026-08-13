#!/bin/bash
# Get the watcher running again, from whatever state it is in.
#
#   ./restart.sh
#
# Safe to run any time, as many times as you like. It reinstalls both
# LaunchAgents, restarts them, and then tells you whether it actually worked
# rather than just claiming success — the whole point is that you run one
# command and get a straight answer.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
WATCHER="com.davidcoyne.ep2026watcher"
WATCHDOG="com.davidcoyne.ep2026watchdog"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

mkdir -p "$AGENTS" "$HOME/.ep2026-watcher/logs"

# Executable bits survive git, but not every way of copying files around.
chmod +x "$REPO/run_watcher.sh" "$REPO/watchdog.sh" 2>/dev/null || true

say "Stopping anything already running"
for label in "$WATCHER" "$WATCHDOG"; do
    launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
done
# A hung Chrome can outlive its parent and hold the profile lock, which makes
# the next start fail in a confusing way. Clear it out.
pkill -f "ep2026-watcher/chrome-profile" 2>/dev/null || true
sleep 2

say "Installing LaunchAgents"
cp "$REPO/launchd/$WATCHER.plist"  "$AGENTS/"
cp "$REPO/launchd/$WATCHDOG.plist" "$AGENTS/"

say "Starting"
launchctl load "$AGENTS/$WATCHER.plist"
launchctl load "$AGENTS/$WATCHDOG.plist"
sleep 5

say "Checking it actually came up"
if launchctl list | grep -q "$WATCHER"; then
    echo "  watcher  : running"
else
    echo "  watcher  : NOT RUNNING — see $HOME/.ep2026-watcher/logs/watcher.err.log"
fi
if launchctl list | grep -q "$WATCHDOG"; then
    echo "  watchdog : running"
else
    echo "  watchdog : NOT RUNNING"
fi

cat <<EOF

  Give it a minute to complete its first poll, then:

      $REPO/run_watcher.sh doctor      # is everything healthy?
      tail -f $HOME/.ep2026-watcher/logs/watcher.log

EOF
