"""Configuration for the EP2026 watcher.

Everything tunable lives here or in the environment. Secrets only ever come
from the environment — nothing sensitive is committed.
"""

import os
from pathlib import Path

# ── The event ────────────────────────────────────────────────────────────────
EVENT_URL = (
    "https://www.ticketmaster.ie"
    "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
    "/event/18006314BD813D3E"
)
EVENT_NAME = "Electric Picnic 2026 - Weekend Camping"

# The id in the URL path. The Inventory Status API wants a "universal" event id
# which may or may not be this same string — `python -m ep_watcher resolve-id`
# looks the real one up via the Discovery API and tells you what to set.
TM_HOST_EVENT_ID = "18006314BD813D3E"
TM_EVENT_ID = os.environ.get("TM_EVENT_ID", TM_HOST_EVENT_ID)

# Quantities to search for, in order. "There aren't enough tickets" is an
# answer about the number you asked for, so this genuinely changes the result:
# the page defaults to 2, and asking for 2 when you'd take 1 manufactures its
# own refusal.
#
# It sweeps more than one on purpose. A search at quantity 2 showed a resale
# listing that a search at quantity 1 minutes later did not, and there are
# three possible reasons — the listing sold, the resale panel filters by
# quantity, or the parser missed it. Sweeping is correct under all three, so
# the watcher does that rather than depending on which one is true.
# Search for one ticket only, by request. This is also the most sensitive
# probe available: "there aren't enough tickets" is an answer about the number
# you asked for, so asking for the smallest number gives the earliest possible
# yes. Anything that exists at all shows up here.
#
# Set WANTED_QUANTITIES=1,2,3 to sweep several instead — each is a separate
# question whose answer does not imply the others.
WANTED_QUANTITIES = [
    int(q) for q in os.environ.get("WANTED_QUANTITIES", "1").split(",") if q.strip()
]
WANTED_QUANTITY = WANTED_QUANTITIES[0]

# ── Alerting ─────────────────────────────────────────────────────────────────
ALERT_TO = os.environ.get("ALERT_TO", "davidcoyne73@gmail.com")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# ── Sources ──────────────────────────────────────────────────────────────────
# Ticketmaster's Inventory Status API. Purpose-built for exactly this question
# and it reports resale separately from primary. Needs an access request —
# see the README. Unset is fine; the browser source carries the load until the
# key arrives, and picks it back up if the key is ever revoked.
TM_API_KEY = os.environ.get("TM_API_KEY")
INVENTORY_API_URL = "https://app.ticketmaster.com/inventory-status/v1/availability"

# The free Discovery API. Instant self-signup at developer.ticketmaster.com,
# 5000 calls/day, 5 req/sec. This is the ONLY source that needs no browser,
# which makes it the only one that can run anywhere other than a machine with
# a real Chrome on it — every ticketmaster.ie endpoint, including the resale
# one the page itself calls, returns 403 "dynamic_block" to plain HTTP.
DISCOVERY_KEY = os.environ.get("TM_DISCOVERY_KEY") or TM_API_KEY
DISCOVERY_ROOT = "https://app.ticketmaster.com/discovery/v2"

# Press "Find Tickets" rather than only reading the page. On by default,
# because it turns out there is no useful read-only mode: verified against the
# live page, a fresh load ends at the search button and renders neither live
# primary stock nor the resale panel. Both only exist in the search response.
# Set PRESS_THE_BUTTON=0 and the watcher will run, report UNKNOWN, and tell
# you it cannot answer the question.
PRESS_THE_BUTTON = os.environ.get("PRESS_THE_BUTTON", "1").lower() in ("1", "true", "yes")

# Chrome profile that holds the Ticketmaster login + bot-check cookies. This
# directory IS the session — back it up, never commit it, and expect to redo
# `login` when Ticketmaster expires it.
PROFILE_DIR = Path(
    os.environ.get("EP_PROFILE_DIR", Path.home() / ".ep2026-watcher" / "chrome-profile")
)

# Playwright drives your real installed Chrome rather than its own bundled
# Chromium build. Same binary a human uses, so the fingerprint isn't the
# obvious tell that got the old cloudscraper version served a 401 wall.
BROWSER_CHANNEL = os.environ.get("EP_BROWSER_CHANNEL", "chrome")

# Headless is NOT an option here, and this default is not conservatism.
# Measured against the live site on 2026-08-13: headless Chrome gets HTTP 403
# on every attempt; the same profile headed gets 200. Turning this on gives
# you a watcher that reports "no tickets" forever and never alerts.
HEADLESS = os.environ.get("EP_HEADLESS", "0").lower() in ("1", "true", "yes")

# Park the (real, headed) Chrome window off the visible desktop so it isn't
# stealing focus every few minutes. Turn off when you want to watch it work,
# and it is turned off automatically for `login` and `calibrate`.
OFFSCREEN = os.environ.get("EP_OFFSCREEN", "1").lower() in ("1", "true", "yes")

PAGE_TIMEOUT_MS = 45_000

# Set EP_USE_BROWSER=0 to run API-sources-only. That is the mode for anywhere
# without a real Chrome — GitHub Actions, a small VPS without a display — and
# it is much weaker: no primary ground truth and no per-listing resale. See
# the hosting section of the README before relying on it.
USE_BROWSER = os.environ.get("EP_USE_BROWSER", "1").lower() in ("1", "true", "yes")

# ── Runtime ──────────────────────────────────────────────────────────────────
REPO_DIR = Path(__file__).parent.parent

# Runtime state lives outside the repo. The old watcher committed and pushed
# its state file on every single run, which is where ~1000 "Update watcher
# state" commits came from — and a workflow that pushes on every run is
# exactly the kind GitHub deprioritises when scheduling. It is local state;
# it belongs in a local directory.
STATE_FILE = Path(
    os.environ.get("EP_STATE_FILE", Path.home() / ".ep2026-watcher" / "state.json")
)
LOG_DIR = Path(os.environ.get("EP_LOG_DIR", Path.home() / ".ep2026-watcher" / "logs"))
DIAG_DIR = Path(os.environ.get("EP_DIAG_DIR", Path.home() / ".ep2026-watcher" / "diagnostics"))

# Consecutive bad runs before the watchdog says something.
WATCHDOG_FAILURE_THRESHOLD = 4

# Re-nag every N hours while still broken. The old watcher latched a single
# "sent" flag and then went quiet for 44 days while failing every run — a
# permanent outage produced less noise than a flaky one. Never again.
WATCHDOG_RENAG_HOURS = 6

# While tickets stay available, re-send the alert this often. One missed push
# shouldn't cost the ticket; a stuck "available" shouldn't send 600 emails.
AVAILABILITY_RENAG_HOURS = 1

# Seconds between polls in `watch`. Jittered by ±25% so the traffic pattern
# isn't a metronome, which is itself something bot detection looks for.
# One search per poll now that only quantity 1 is checked, so this can come
# back down without raising the overall request rate.
POLL_INTERVAL_SECONDS = int(os.environ.get("EP_POLL_SECONDS", "180"))

# Send an "still nothing, still trying" email this often while there is no
# ticket. Its real job is proving the watcher is alive: silence from a watcher
# is ambiguous, and the previous one exploited that ambiguity for 44 days. A
# clock that has to keep ticking cannot fail quietly.
#
# The clock resets whenever a real availability alert goes out — if a ticket
# turned up, that email already told the story.
HEARTBEAT_HOURS = float(os.environ.get("EP_HEARTBEAT_HOURS", "1"))

# Floor on the interval when PRESS_THE_BUTTON is on. Each press is a real
# reserve attempt against live inventory, not a page read, so it runs at a
# human-plausible cadence rather than the read-only rate. Raise the read-only
# rate freely; think before lowering this one.
PRESS_MIN_INTERVAL_SECONDS = int(os.environ.get("EP_PRESS_MIN_SECONDS", "600"))
