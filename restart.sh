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
#
# The WATCHER's profile only, and the anchor on the end of that pattern is
# load-bearing. `pkill -f` matches a substring of the whole command line, so a
# bare "ep2026-watcher/chrome-profile" also matched chrome-profile-buy — the
# signed-in browser that is deliberately left open while a basket is held,
# because closing it is exactly what releases the hold. This script is what
# `doctor` prints as the fix for half its failure lines, so running it during
# a live hold would have thrown away the ticket the watcher had just caught.
# Verified against a real process on 2026-08-19: the bare pattern matches a
# chrome-profile-buy command line, the anchored one does not.
# Exercisable without bouncing anything, the same way watchdog.sh is. The
# decision below about the BUYING browser is the one worth testing — it is the
# only line in this script that can destroy a ticket — and a test that has to
# unload launchd to reach it would never be written.
DRY_RUN="${EP_RESTART_DRY_RUN:-}"

if [ -z "$DRY_RUN" ]; then
    pkill -f "ep2026-watcher/chrome-profile( |$)" 2>/dev/null || true
fi

# The buying browser is a different question, and it changed on 2026-08-20
# when that browser started being kept WARM — opened at watcher startup and
# parked, rather than launched only when a listing appears.
#
# Before that, an open buying browser meant one thing: a hold waiting to be
# paid for. Leaving it alone was simply correct. Now it usually means an idle
# warm browser belonging to the watcher this script is about to replace — and
# leaving THAT alone orphans it. It keeps the profile lock for ever, the new
# watcher's warm browser cannot start, and the feature silently degrades to
# the cold starts it was built to avoid.
#
# So the two cases have to be told apart, and the state file is what tells
# them apart — the same marker watchdog.sh reads before deciding it may
# restart a watcher at all. A live hold is untouchable. An idle warm browser
# is this script's to clean up.
HOLDING=$(/usr/bin/python3 - "${EP_STATE_FILE:-$HOME/.ep2026-watcher/state.json}" <<'PY' 2>/dev/null
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1]) as f:
        until = json.load(f).get("hold_until")
    if not until:
        print(0)
    else:
        left = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds()
        print(int(max(0, left) // 60))
except Exception:
    # Cannot tell? Then treat it as held. Being wrong in this direction
    # costs a warm browser; being wrong the other way costs a ticket.
    print(999)
PY
)

if [ -n "$HOLDING" ] && [ "$HOLDING" -gt 0 ] 2>/dev/null; then
    BUY_VERDICT="KEEP"
else
    BUY_VERDICT="KILL"
fi

if [ -n "$DRY_RUN" ]; then
    echo "BUY_BROWSER=$BUY_VERDICT"
    say "dry run — nothing stopped, nothing started"
    exit 0
fi

if pgrep -f "ep2026-watcher/chrome-profile-buy" >/dev/null 2>&1; then
    if [ "$BUY_VERDICT" = "KEEP" ]; then
        echo "  note: a ticket is HELD in the buying browser (${HOLDING} min left)."
        echo "        It has been left alone. Finish that checkout."
    else
        echo "  note: closing the idle warm buying browser so the new watcher can open its own."
        pkill -f "ep2026-watcher/chrome-profile-buy" 2>/dev/null || true
        sleep 1
    fi
fi
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
