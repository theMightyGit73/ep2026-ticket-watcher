"""Configuration for the EP2026 watcher.

Everything tunable lives here or in the environment. Secrets only ever come
from the environment — nothing sensitive is committed.
"""

import os
from pathlib import Path

# ── The event ────────────────────────────────────────────────────────────────
class Event:
    """One ticket page to watch.

    `slug` keys this event's own availability history in state.json. Without
    per-event history, a listing appearing on one page would update the
    shared "last seen" values and silence the alert for the other — the two
    are separate products and have to be tracked separately.
    """

    def __init__(self, slug: str, name: str, url: str, match_words=(),
                 tm_event_id: str = ""):
        self.slug = slug
        self.name = name
        self.url = url
        #: Words that identify this event in the Discovery index, lowercase.
        self.match_words = tuple(w.lower() for w in match_words)
        #: This page's own id for the Inventory Status API, if one is known.
        #: Empty means that source cannot answer about this event and must say
        #: so — see sources/inventory_api.py. Answering with another page's
        #: inventory would be a confident statement about the wrong ticket.
        self.tm_event_id = tm_event_id

    def __repr__(self):
        return f"Event({self.slug})"


EVENTS = [
    Event(
        slug="weekend-camping",
        name="Electric Picnic 2026 - Weekend Camping",
        url=(
            "https://www.ticketmaster.ie"
            "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
            "/event/18006314BD813D3E"
        ),
        match_words=("electric picnic", "weekend"),
        tm_event_id=os.environ.get("TM_EVENT_ID", "18006314BD813D3E"),
    ),
    # The instalment-plan listing for the same festival. A separate page with
    # its own inventory and its own resale panel, so it needs watching in its
    # own right — a ticket can appear on one and not the other.
    Event(
        slug="weekend-camping-instalment",
        name="Electric Picnic 2026 - Weekend Camping Instalment Plan",
        url=(
            "https://www.ticketmaster.ie"
            "/electric-picnic-2026-weekend-camping-instalment-co-laois-28-08-2026"
            "/event/18006314CFB4A99E"
        ),
        match_words=("electric picnic", "weekend", "instalment"),
    ),
]

# The first event stays the default for anything that still speaks in the
# singular (the `login` and `calibrate` commands, mainly).
#
# Nothing that alerts should reach for these. An alert must take its event
# from the reading that produced it, or it will name the wrong page — see
# notify._event_of. These two are the fallback for commands that genuinely
# operate on one page, not a convenience for the alerting path.
EVENT_URL = EVENTS[0].url
EVENT_NAME = EVENTS[0].name

# What to call the whole watch in an email that covers every page at once.
#
# Set explicitly rather than derived from the event names. The two pages share
# a prefix that is itself one of their names ("...Weekend Camping" is a prefix
# of "...Weekend Camping Instalment Plan"), so deriving a common label would
# title a two-page report with the first page's name — the exact confusion
# this exists to remove.
WATCH_LABEL = os.environ.get("EP_WATCH_LABEL", "Electric Picnic 2026")

# Last day the watcher runs. The festival opens on the 28th, so a ticket
# found that morning is still usable — this is the last watching day, and it
# stops at the end of it. Set EP_STOP_AFTER=2026-08-27 to stop as the 28th
# begins instead.
#
# This exists because nothing here stops on its own, and an unattended
# watcher outliving its event is how you end up with a cron job still mailing
# you about a festival two years gone.
STOP_AFTER_DATE = os.environ.get("EP_STOP_AFTER", "2026-08-28")

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

# How the Discovery source recognises the wanted ticket among the indexed
# Electric Picnic events. Name matching, not id: the id in the ticketmaster.ie
# URL is a host id that Discovery does not recognise (a direct lookup 404s).
DISCOVERY_MATCH_WORDS = ("electric picnic", "weekend")
# The campervan passes stay indexed permanently and are not the wanted ticket.
# Without excluding them, the source would report "available" forever.
DISCOVERY_EXCLUDE_WORDS = ("campervan", "caravan")

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

# While a listing is actually live, remind far more often than hourly.
#
# The hourly figure was set before we knew how long these last. Measured on
# 2026-08-17, a listing was present at 07:49 and still there at 08:01 — ten to
# twenty minutes, not the five originally assumed. So an hourly reminder is
# useless for the case it exists to cover: a missed push meant the next word
# came long after the ticket had gone. Four minutes means a listing that lives
# fifteen gets three or four chances to reach you.
LIVE_RENAG_MINUTES = float(os.environ.get("EP_LIVE_RENAG_MINUTES", "4"))

# Seconds between polls in `watch`. Jittered by ±25% so the traffic pattern
# isn't a metronome, which is itself something bot detection looks for.
# Raised to 10 minutes after a 180s cadence got this client rate-limited
# during testing — roughly 30 searches in an afternoon was enough to start
# drawing HTTP 403 instead of the real page.
#
# The arithmetic is the argument. At 180s, two weeks is ~6,700 searches; at
# 600s it is ~2,000. Neither is a human, but only one of them is quiet enough
# to keep working, and a watcher that gets itself blocked on day two catches
# nothing on day nine. Lower it during a known onsale if you want, and accept
# that it may cost you the rest of the fortnight.
# 5 minutes during the day. Raised from 10 after a real listing on
# 2026-08-13 lived roughly one poll interval: detected at 22:09, gone before
# it could be opened. At 10 minutes a listing that short is missed outright
# about half the time.
#
# The cost is ~12 searches an hour rather than 6, against the ~20/hour that
# got the home IP flagged. Acceptable now that the watcher alternates
# networks every 3 hours, resets its browser profile on a block, and backs
# off exponentially — none of which existed when that block happened.
_POLL_PER_EVENT_SECONDS = int(os.environ.get("EP_POLL_SECONDS", "300"))


def poll_interval() -> int:
    """Seconds between polls, scaled so total request volume stays flat.

    Every event is searched on every poll, so N events means N searches per
    cycle. Holding the interval fixed while adding a second event would
    double the hourly rate to ~24 searches — above the ~20/hour that got the
    home IP flagged, which is the one outcome that costs the ticket outright.
    Scaling instead trades per-event frequency for coverage, honestly and
    visibly, rather than quietly buying the second event with risk.

    Set EP_POLL_SECONDS to the per-event budget; the cycle is that times the
    number of events.
    """
    return _POLL_PER_EVENT_SECONDS * max(1, len(EVENTS))


POLL_INTERVAL_SECONDS = poll_interval()

# Overnight, poll far less often.
#
# The reasoning is about what the watcher is *for*. Its value is a headstart,
# and a headstart is worth nothing at 3am — you cannot act on a resale
# listing while asleep, and those listings last about five minutes. So the
# overnight hours buy almost no coverage while quietly accumulating request
# volume on whichever connection is in use, unattended, with nobody awake to
# notice a block.
#
# Slowing to 30 minutes cuts the overnight load on that IP by two thirds and
# leaves it fresh for the morning, which is when a headstart actually counts.
# Local time, not UTC. Set EP_NIGHT_POLL_SECONDS=0 to disable.
NIGHT_POLL_SECONDS = int(os.environ.get("EP_NIGHT_POLL_SECONDS", "1800"))
NIGHT_START_HOUR = int(os.environ.get("EP_NIGHT_START_HOUR", "0"))
NIGHT_END_HOUR = int(os.environ.get("EP_NIGHT_END_HOUR", "7"))


def is_night(now=None) -> bool:
    """Is it currently the quiet overnight window, in local time?"""
    from datetime import datetime

    hour = (now or datetime.now()).hour
    if NIGHT_START_HOUR == NIGHT_END_HOUR:
        return False
    if NIGHT_START_HOUR < NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR
    # Window wraps past midnight, e.g. 23:00-07:00.
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def poll_interval_now(daytime_interval=None) -> tuple:
    """(seconds, is_night) for the next poll."""
    day = daytime_interval or POLL_INTERVAL_SECONDS
    if NIGHT_POLL_SECONDS and is_night():
        return max(NIGHT_POLL_SECONDS, day), True
    return day, False


# How long to wait for a pressed search to resolve into an answer.
#
# Longer overnight, because that is when the page is slow. Every observed
# "search did not resolve within the timeout" — five of them across two
# nights, on 2026-08-15, -16 and -17 — fell between 22:08 and 01:00, against
# hundreds of daytime polls with none at all. Each one costs a resale-blind
# poll on both pages, which is a listing that could not have been seen.
#
# Raising it is close to free, and that asymmetry is the argument. A search
# that resolves normally returns the moment its marker appears — a few
# seconds — so this ceiling is only ever reached by a poll that was going to
# fail anyway. The extra wait is spent on failures, never on healthy polls.
#
# Being honest about the evidence: five clustered observations say the page
# is slow then, not that 90 seconds is enough. If timeouts persist at this
# value, the cause is something other than slowness and a bigger number will
# not find it.
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("EP_SEARCH_TIMEOUT", "45"))
NIGHT_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("EP_NIGHT_SEARCH_TIMEOUT", "90"))


def search_timeout(now=None) -> int:
    """Seconds to wait for a search, by time of day.

    Keyed on the night *window* rather than on NIGHT_POLL_SECONDS, because
    the two describe different things: the window is when Ticketmaster is
    slow, the poll setting is how often we choose to ask. Turning the
    overnight slowdown off should not also remove the extra patience.
    """
    return NIGHT_SEARCH_TIMEOUT_SECONDS if is_night(now) else SEARCH_TIMEOUT_SECONDS

# How long to sleep after an HTTP 403, doubling on each consecutive block up
# to the cap, and reset on the first good read.
BLOCKED_BACKOFF_SECONDS = int(os.environ.get("EP_BACKOFF_SECONDS", "1800"))
BLOCKED_BACKOFF_MAX_SECONDS = int(os.environ.get("EP_BACKOFF_MAX_SECONDS", "10800"))

# Send an "still nothing, still trying" email this often while there is no
# ticket. Its real job is proving the watcher is alive: silence from a watcher
# is ambiguous, and the previous one exploited that ambiguity for 44 days. A
# clock that has to keep ticking cannot fail quietly.
#
# The clock resets whenever a real availability alert goes out — if a ticket
# turned up, that email already told the story.
HEARTBEAT_HOURS = float(os.environ.get("EP_HEARTBEAT_HOURS", "1"))

# How long the Mac may go without checking in before GitHub declares it down.
# Generously above the poll interval and the overnight slowdown, so a slow
# poll or a brief network drop never triggers it — this alert must only fire
# when the Mac is genuinely not running.
MAC_SILENT_HOURS = float(os.environ.get("EP_MAC_SILENT_HOURS", "1.5"))

# ── Alternating between home Wi-Fi and the phone hotspot ─────────────────────
# The watcher asks David to switch the MacBook's network after this long, or
# this many searches, whichever comes first. Splitting the volume across two
# connections keeps either from accumulating enough to be rate-limited, and
# leaves one healthy connection to buy with if the other does get flagged.
#
# At the default 10-minute cadence, 6 hours is ~36 searches — comfortably
# below the ~30-in-an-afternoon that got the home IP flagged on 2026-08-13,
# with the search cap as a backstop if the cadence is ever lowered.
# Every 3 hours, or 30 searches. Note what this does and does not buy: the
# daily total per connection is set by the poll rate, not the switch rate —
# 144 searches a day split two ways is ~72 each however often you alternate.
# What switching more often lowers is how many land on one IP inside any
# given hour, which is what a rate limit actually measures.
NETWORK_ROTATE_HOURS = float(os.environ.get("EP_ROTATE_HOURS", "3"))
NETWORK_ROTATE_SEARCHES = int(os.environ.get("EP_ROTATE_SEARCHES", "30"))

# Optional. If set, this IP is labelled "home Wi-Fi" in the emails. Left
# unset, the first connection the watcher ever sees is assumed to be home,
# which is right if you start it at home.
HOME_NETWORK_IP = os.environ.get("EP_HOME_IP")

# Throw the browser profile away and rebuild it after this many minutes.
#
# The bot-check cookies age out. Across 28 blocks in six days every single one
# was cleared by a fresh profile on the first attempt, and the exponential
# backoff behind that reset was never reached once — so the wall lives in the
# profile, not in the IP. Waiting for it costs two resale-blind readings and a
# wasted cycle each time; stepping around it costs one cold page load during a
# sleep window.
#
# 90 minutes sits under the shortest daytime gap observed between blocks (64
# minutes is the floor; the common cluster is around two hours), so it lands
# ahead of most of them. Set EP_PROFILE_MAX_AGE=0 to go back to waiting.
PROFILE_MAX_AGE_MINUTES = float(os.environ.get("EP_PROFILE_MAX_AGE", "90"))

# Hard floor on the interval, regardless of what EP_POLL_SECONDS says.
#
# There is a real tension here and it is worth stating rather than hiding. A
# resale listing observed during testing lived about five minutes, so a slow
# cadence genuinely misses tickets. But a fast cadence gets the client
# rate-limited, and a blocked watcher misses every ticket, not some of them.
# Two minutes is the floor because sustained polling faster than that is what
# produced the 403s; the default sits well above it.
PRESS_MIN_INTERVAL_SECONDS = int(os.environ.get("EP_PRESS_MIN_SECONDS", "120"))
