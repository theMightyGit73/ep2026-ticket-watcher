#!/bin/bash
# Restart the watcher if it has stopped doing anything.
#
# launchd already restarts the watcher when it *crashes*. This covers the
# other failure: a process that is still alive but wedged — a hung Chrome, a
# stuck network read — which keeps its PID, satisfies every check launchd
# makes, and quietly does nothing. The only symptom would be silence, and
# silence is exactly what this project refuses to leave ambiguous.
#
# The liveness signal is last_check_at in state.json, written on every poll
# whether it succeeded or failed. If that timestamp stops advancing, the
# watcher is not working regardless of what launchd thinks.
#
# Run every 15 minutes by com.davidcoyne.ep2026watchdog.plist.
set -uo pipefail

LABEL="com.davidcoyne.ep2026watcher"
# Overridable so the restart logic can be exercised against a fixture rather
# than only ever being tested by waiting for a real hang.
STATE="${EP_STATE_FILE:-$HOME/.ep2026-watcher/state.json}"
# Overridable for the same reason STATE is. The test suite runs this script
# against fixture states, and with the log hardcoded those runs wrote lines
# like "last poll was 90 min ago — restarting the watcher" straight into the
# operational log. That is the log you read while diagnosing a real hang, so
# filling it with alarming sentences about fixtures is actively harmful.
LOG="${EP_WATCHDOG_LOG:-$HOME/.ep2026-watcher/logs/watchdog.log}"
STALE_MINUTES="${EP_STALE_MINUTES:-45}"

mkdir -p "$(dirname "$LOG")"
say() { echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] $*" >> "$LOG"; }

# Past the stop date? Then stopping is correct and must not be "repaired".
STOP_AFTER="${EP_STOP_AFTER:-2026-08-28}"
if [[ "$(date -u +%F)" > "$STOP_AFTER" ]]; then
    exit 0
fi

if [ ! -f "$STATE" ]; then
    say "no state file yet — watcher may still be starting; leaving it alone"
    exit 0
fi

# Age of the last poll, in minutes.
LAST=$(/usr/bin/python3 - "$STATE" <<'PY' 2>/dev/null
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1]) as f:
        ts = json.load(f).get("last_check_at")
    if not ts:
        print(-1)
    else:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        print(int(age // 60))
except Exception:
    print(-1)
PY
)

if [ -z "$LAST" ] || [ "$LAST" -lt 0 ] 2>/dev/null; then
    say "could not read last_check_at — leaving it alone"
    exit 0
fi

if [ "$LAST" -le "$STALE_MINUTES" ]; then
    exit 0   # healthy, and deliberately silent so the log stays readable
fi

say "last poll was ${LAST} min ago (limit ${STALE_MINUTES}) — restarting the watcher"

# Lets the decision be exercised without actually bouncing the service, so
# the test suite can check "would it restart?" without disrupting a watcher
# that is working. Also handy for confirming the threshold by hand.
if [ "${EP_WATCHDOG_DRY_RUN:-}" = "1" ]; then
    echo "WOULD_RESTART"
    say "dry run — no restart issued"
    exit 0
fi
if launchctl kickstart -k "gui/$(id -u)/$LABEL" >>"$LOG" 2>&1; then
    say "restart issued"
else
    # kickstart fails if the job is not loaded at all — load it instead.
    say "kickstart failed; trying to load the agent from scratch"
    launchctl load "$HOME/Library/LaunchAgents/$LABEL.plist" >>"$LOG" 2>&1 \
        && say "agent loaded" || say "could not load agent — needs a human"
fi
