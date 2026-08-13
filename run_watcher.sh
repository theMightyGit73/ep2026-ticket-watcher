#!/bin/bash
# Wrapper the LaunchAgent calls. Keeps secrets out of the plist: put them in
# ~/.ep2026-watcher/env (chmod 600) and this sources them at start.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.ep2026-watcher/env"
LOG_DIR="$HOME/.ep2026-watcher/logs"

# Python buffers stdout when it is a file rather than a terminal, so under
# launchd the log stays empty for ages and the service looks dead when it is
# working perfectly. Unbuffered costs nothing here and makes `tail -f` honest.
export PYTHONUNBUFFERED=1

mkdir -p "$LOG_DIR"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "WARNING: $ENV_FILE not found — email alerts will not send." >&2
fi

exec "$REPO/.venv/bin/python" -m ep_watcher "${@:-watch}"
