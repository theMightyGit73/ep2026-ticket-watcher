#!/bin/bash
# Container entrypoint.
#
# Two jobs: pick the browser the image actually managed to install, and give
# it a display. xvfb-run supplies a virtual X server, which is what lets
# Chrome run *headed* on a machine with no monitor — the distinction the whole
# design rests on, since headless Chrome is rejected with an HTTP 403 every
# time and headed Chrome is not.
set -euo pipefail

export EP_BROWSER_CHANNEL="${EP_BROWSER_CHANNEL:-$(cat /etc/ep-browser-channel 2>/dev/null || echo chrome)}"

if [ "$EP_BROWSER_CHANNEL" != "chrome" ]; then
    echo "WARNING: using '$EP_BROWSER_CHANNEL' rather than Google Chrome." >&2
    echo "         Expect ticketmaster.ie to block this. Use an x86-64 host." >&2
fi

# A headed browser on a virtual display, never headless.
export EP_HEADLESS=0
export EP_OFFSCREEN=0

exec xvfb-run -a --server-args="-screen 0 1440x900x24" \
     python -m ep_watcher "$@"
