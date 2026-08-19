"""Configuration for the EP2026 watcher.

Everything tunable lives here or in the environment. Secrets only ever come
from the environment — nothing sensitive is committed.
"""

import os
import random
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
                 tm_event_id: str = "", poll_seconds: int = 0,
                 poll_min_seconds: int = 0, poll_max_seconds: int = 0,
                 peak_min_seconds: int = 0, peak_max_seconds: int = 0,
                 offpeak_min_seconds: int = 0, offpeak_max_seconds: int = 0,
                 secure: bool = True):
        self.peak_min_seconds, self.peak_max_seconds = peak_min_seconds, peak_max_seconds
        self.offpeak_min_seconds = offpeak_min_seconds
        self.offpeak_max_seconds = offpeak_max_seconds
        #: May the buyer open a signed-in browser and hold this one?
        #:
        #: Per page, because "tell me about it" and "grab it for me" are not
        #: the same instruction. The Early Entry Pass is an add-on that
        #: Ticketmaster says is only valid alongside a Weekend Ticket, so
        #: automatically holding one — under his account, with a countdown
        #: running, pulling him to the laptop — would be spending his
        #: attention on something he cannot use until the real ticket exists.
        #: It is still watched and still alerted on; it is just not grabbed.
        self.secure = secure
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

        #: The gap between searches of this page is drawn fresh from
        #: [poll_min_seconds, poll_max_seconds] after every search, rather
        #: than being a fixed number. Two reasons, and the second is the one
        #: that matters:
        #:
        #:   * A metronome is a bot signature. A page hit at 12:00:03,
        #:     12:06:03, 12:12:04 is describing itself; a range is not.
        #:   * The average gap can be shortened without the peak request rate
        #:     rising as much as a fixed cadence at the same average would,
        #:     because the draws spread rather than stacking.
        #:
        #: `poll_seconds` remains the MEAN of that range and is what the
        #: budget arithmetic uses, so searches_per_hour() still answers the
        #: question that actually matters: how much traffic is this sending.
        # A page may be given an ordinary range, or only peak/off-peak ranges,
        # or neither. Falling through to DEFAULT_EVENT_POLL_SECONDS when only
        # the windowed ranges were supplied is a trap: gap_range() would do
        # the right thing while poll_seconds — and therefore
        # searches_per_hour() — reported the default. The Early Entry Pass hit
        # exactly that on the day it was added, claiming 13.3 searches an hour
        # for a page actually polled every half hour.
        if not (poll_min_seconds or poll_max_seconds) and peak_min_seconds:
            poll_min_seconds, poll_max_seconds = peak_min_seconds, peak_max_seconds

        if poll_min_seconds or poll_max_seconds:
            lo = poll_min_seconds or poll_max_seconds
            hi = poll_max_seconds or poll_min_seconds
            self.poll_min_seconds, self.poll_max_seconds = min(lo, hi), max(lo, hi)
            self.poll_seconds = (self.poll_min_seconds + self.poll_max_seconds) // 2
        else:
            self.poll_seconds = poll_seconds or DEFAULT_EVENT_POLL_SECONDS
            self.poll_min_seconds = self.poll_max_seconds = self.poll_seconds

    def gap_range(self, now=None) -> tuple:
        """(min, max) seconds for this page at this time of day.

        Peak and off-peak are the same budget spent differently, not extra
        spending — see PEAK_START_HOUR. A page with no peak range configured
        simply keeps its ordinary one all day.
        """
        if is_peak(now) and self.peak_min_seconds:
            return self.peak_min_seconds, self.peak_max_seconds
        if not is_peak(now) and self.offpeak_min_seconds:
            return self.offpeak_min_seconds, self.offpeak_max_seconds
        return self.poll_min_seconds, self.poll_max_seconds

    def next_gap(self, now=None) -> int:
        """How long to wait before searching this page again.

        Drawn ONCE per search and then stored on the event's state — never
        re-drawn while waiting. Re-drawing on each tick of the watch loop
        would quietly collapse the range to its floor: with a fresh draw every
        30 seconds, the page becomes due as soon as any one draw lands low,
        so the effective interval is the minimum of many draws rather than a
        sample from the range. See state.note_event_polled().
        """
        lo, hi = self.gap_range(now)
        if hi <= lo:
            return lo
        return random.randint(lo, hi)

    @property
    def fastest_gap_seconds(self) -> int:
        """The shortest gap this page could ever draw, across every window.

        What the watch loop's tick has to keep up with. Taking only the
        ordinary range would leave the peak window's faster draws unreachable.
        """
        return min(g for g in (self.poll_min_seconds, self.peak_min_seconds,
                               self.offpeak_min_seconds) if g)

    @property
    def searches_per_hour(self) -> float:
        return 3600.0 / self.poll_seconds

    def __repr__(self):
        return f"Event({self.slug})"


# How often each page is searched, in seconds.
#
# These are not equal, and the evidence says they should not be. Of the nine
# resale sightings recorded between 13 and 18 August, EIGHT were on the
# standard Weekend Camping page and one was on the instalment plan — yet both
# pages were being searched on every cycle, splitting the budget in half for a
# 8:1 difference in yield.
#
# Rebalancing costs nothing. At one search every 6 minutes the standard page
# takes 10 searches an hour and the instalment plan, at one every 30 minutes,
# takes 2 — a total of 12 an hour, exactly what the even split was already
# spending. What changes is where the attention goes.
#
# The gain comes from how short these listings are. Seven of the eight
# distinct sightings were visible on exactly one poll and gone by the next,
# which is the signature of a lifetime at or below the poll interval: fitting
# that ratio gives a mean life of about 4.6 minutes, and a detection chance of
# roughly 40% at a 10-minute cycle. Moving the busy page to 6 minutes raises
# its share to about 56%. Weighted by where listings actually appear, that is
# close to a third more finds for the same number of requests.
#
# Since 2026-08-19 each page's gap is a RANGE rather than a single number, and
# is drawn fresh after every search. David asked for 3-6 minutes on the
# standard page. Two things that buys:
#
#   * The traffic stops being a metronome. A fixed 360s cadence prints a
#     recognisable pattern — the ±25% jitter on the loop's own sleep did not
#     fix that, because the page was still searched the moment it came due.
#   * The mean gap drops from 360s to 270s, so a listing with a ~4.6 minute
#     life is more likely to be seen at all.
#
# The cost is real and is the thing to watch: the standard page goes from 10
# searches an hour to ~13.3, and the total from 12/hour to ~15.3/hour, against
# the ~20/hour that got the home connection flagged in development. It is
# still under that line, but by less than it was.
STANDARD_POLL_MIN_SECONDS = int(os.environ.get("EP_STANDARD_POLL_MIN", "180"))
STANDARD_POLL_MAX_SECONDS = int(os.environ.get("EP_STANDARD_POLL_MAX", "360"))

# Sellers keep daylight hours, so the watcher should too.
#
# David suggested 15:00-22:00 on 2026-08-19. The eight resale sightings
# recorded to that date say the productive window is wider and earlier — all
# eight fell between 08:00 and 20:00 local, and none overnight:
#
#     08:49  10:09  11:35  14:14  14:32  17:02  18:33  19:57   (local)
#
# Measured as sightings-per-hour-of-clock, 10:00-20:00 is the best window
# available: 7 of 8 in 42% of the day, an enrichment of 2.1x. His 15:00-22:00
# holds only 3 of 8, an enrichment of 1.29x. Eight is a small number and this
# should be revisited as more arrive — hence the environment variables.
#
# The budget is REDISTRIBUTED, not increased. Off-peak daytime slows down by
# as much as the peak speeds up, so the day's total is about 248 searches
# against the 274 the flat cadence was spending. Peak instantaneous load is
# ~17/hour, still under the ~20/hour that drew a block in development.
PEAK_START_HOUR = int(os.environ.get("EP_PEAK_START_HOUR", "10"))
PEAK_END_HOUR = int(os.environ.get("EP_PEAK_END_HOUR", "20"))

STANDARD_PEAK_MIN_SECONDS = int(os.environ.get("EP_STANDARD_PEAK_MIN", "180"))
STANDARD_PEAK_MAX_SECONDS = int(os.environ.get("EP_STANDARD_PEAK_MAX", "300"))
STANDARD_OFFPEAK_MIN_SECONDS = int(os.environ.get("EP_STANDARD_OFFPEAK_MIN", "300"))
STANDARD_OFFPEAK_MAX_SECONDS = int(os.environ.get("EP_STANDARD_OFFPEAK_MAX", "600"))

# The Early Entry Pass, added 2026-08-19. Watched on the slowest clock of the
# three, and the reasons are worth stating because they are not obvious:
#
#   * It is an ADD-ON, not a ticket. Ticketmaster's own note says "Early Entry
#     passes are only valid with a Weekend Ticket", so it is worth nothing
#     until the thing this whole project exists to find has been found.
#   * It was on general sale at €39.40 when David added it, with stock showing
#     and a four-per-order limit. A page that is selling is not a page that
#     needs watching every three minutes; the question here is "has it sold
#     out and come back", not "did a resale listing flash past".
#
# 30-60 minutes costs ~1.3 searches an hour, taking the peak from 17.0 to
# 18.3 — still under the ~20/hour that drew a block.
EARLY_ENTRY_PEAK_MIN_SECONDS = int(os.environ.get("EP_EARLY_PEAK_MIN", "1800"))
EARLY_ENTRY_PEAK_MAX_SECONDS = int(os.environ.get("EP_EARLY_PEAK_MAX", "3600"))
EARLY_ENTRY_OFFPEAK_MIN_SECONDS = int(os.environ.get("EP_EARLY_OFFPEAK_MIN", "3600"))
EARLY_ENTRY_OFFPEAK_MAX_SECONDS = int(os.environ.get("EP_EARLY_OFFPEAK_MAX", "5400"))

INSTALMENT_PEAK_MIN_SECONDS = int(os.environ.get("EP_INSTALMENT_PEAK_MIN", "1200"))
INSTALMENT_PEAK_MAX_SECONDS = int(os.environ.get("EP_INSTALMENT_PEAK_MAX", "2400"))
INSTALMENT_OFFPEAK_MIN_SECONDS = int(os.environ.get("EP_INSTALMENT_OFFPEAK_MIN", "2400"))
INSTALMENT_OFFPEAK_MAX_SECONDS = int(os.environ.get("EP_INSTALMENT_OFFPEAK_MAX", "3600"))


def is_peak(now=None) -> bool:
    """Is it currently the window in which listings actually appear?

    Local time, like is_night(), because sellers keep local hours. Night wins
    over peak if the two are ever configured to overlap — the overnight
    slowdown exists to keep the watcher quiet while nobody is listing, and a
    peak window should never be able to undo that.
    """
    from datetime import datetime

    if is_night(now):
        return False
    hour = (now or datetime.now()).hour
    if PEAK_START_HOUR == PEAK_END_HOUR:
        return False
    if PEAK_START_HOUR < PEAK_END_HOUR:
        return PEAK_START_HOUR <= hour < PEAK_END_HOUR
    return hour >= PEAK_START_HOUR or hour < PEAK_END_HOUR
# The instalment plan is randomised too, around its existing 30-minute mean.
# One of the nine sightings to date was on this page, so it keeps its small
# share of the budget; the range only stops it being predictable.
INSTALMENT_POLL_MIN_SECONDS = int(os.environ.get("EP_INSTALMENT_POLL_MIN", "1200"))
INSTALMENT_POLL_MAX_SECONDS = int(os.environ.get("EP_INSTALMENT_POLL_MAX", "2400"))

#: Kept as the mean of the standard range, for anything that still wants a
#: single number (the banner, and any page added without its own range).
STANDARD_POLL_SECONDS = (STANDARD_POLL_MIN_SECONDS + STANDARD_POLL_MAX_SECONDS) // 2
INSTALMENT_POLL_SECONDS = (INSTALMENT_POLL_MIN_SECONDS + INSTALMENT_POLL_MAX_SECONDS) // 2
DEFAULT_EVENT_POLL_SECONDS = STANDARD_POLL_SECONDS

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
        poll_min_seconds=STANDARD_POLL_MIN_SECONDS,
        poll_max_seconds=STANDARD_POLL_MAX_SECONDS,
        peak_min_seconds=STANDARD_PEAK_MIN_SECONDS,
        peak_max_seconds=STANDARD_PEAK_MAX_SECONDS,
        offpeak_min_seconds=STANDARD_OFFPEAK_MIN_SECONDS,
        offpeak_max_seconds=STANDARD_OFFPEAK_MAX_SECONDS,
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
        poll_min_seconds=INSTALMENT_POLL_MIN_SECONDS,
        poll_max_seconds=INSTALMENT_POLL_MAX_SECONDS,
        peak_min_seconds=INSTALMENT_PEAK_MIN_SECONDS,
        peak_max_seconds=INSTALMENT_PEAK_MAX_SECONDS,
        offpeak_min_seconds=INSTALMENT_OFFPEAK_MIN_SECONDS,
        offpeak_max_seconds=INSTALMENT_OFFPEAK_MAX_SECONDS,
    ),
    # The Early Entry Pass — campsite access from 2pm on the Thursday. A
    # separate page with its own inventory, and NOT a ticket: Ticketmaster's
    # own note reads "Early Entry passes are only valid with a Weekend
    # Ticket". Watched and alerted on like the others, but never secured
    # automatically — see Event.secure.
    Event(
        slug="early-entry",
        name="Electric Picnic 2026 - Early Entry Pass",
        url=(
            "https://www.ticketmaster.ie"
            "/electric-picnic-2026-early-entry-pass-co-laois-27-08-2026"
            "/event/18006314E36BAC7B"
        ),
        match_words=("electric picnic", "early entry"),
        tm_event_id="18006314E36BAC7B",
        peak_min_seconds=EARLY_ENTRY_PEAK_MIN_SECONDS,
        peak_max_seconds=EARLY_ENTRY_PEAK_MAX_SECONDS,
        offpeak_min_seconds=EARLY_ENTRY_OFFPEAK_MIN_SECONDS,
        offpeak_max_seconds=EARLY_ENTRY_OFFPEAK_MAX_SECONDS,
        secure=False,
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

# ── Securing a find ──────────────────────────────────────────────────────────
# From 2026-08-19 the watcher may do more than report: on a resale find it can
# open a SECOND browser, signed in, click into the listing and hold it in a
# basket — then stop, and hand the live hold to David to pay for.
#
# The split into two browsers is the whole point and is David's design. The
# watcher's own browser stays signed out and does all the polling, so the
# ~140 page-loads a day are anonymous and a block costs nothing but a profile
# reset. The account only ever appears at the moment a real listing exists —
# six times on 2026-08-18, against 140 polls. Do not be tempted to collapse
# these into one signed-in session for simplicity; that trades the account's
# safety for a few seconds of browser startup.
#
# Default OFF. This spends nothing and signs nothing, but it does put his
# account in front of Ticketmaster's bot detection, so it must be turned on
# deliberately rather than inherited by a fresh checkout.
SECURE_ON_FIND = os.environ.get("EP_SECURE_ON_FIND", "0").lower() in ("1", "true", "yes")

# A separate user-data-dir from PROFILE_DIR, and not negotiable: Chrome takes
# an exclusive lock on a profile directory, so the buying browser cannot share
# the watcher's while the watcher is running. Keeping them apart is also what
# keeps the signed-in cookies out of the browser that does the polling.
BUY_PROFILE_DIR = Path(
    os.environ.get("EP_BUY_PROFILE_DIR",
                   Path.home() / ".ep2026-watcher" / "chrome-profile-buy")
)

# How long to keep trying to secure one listing before giving up and just
# alerting. Past this the listing is almost certainly in someone else's
# basket, and the honest thing is to tell him rather than keep clicking.
SECURE_TIMEOUT_SECONDS = int(os.environ.get("EP_SECURE_TIMEOUT_SECONDS", "45"))

# How long David has once a ticket is held. Used only to word the alert,
# never to decide anything.
#
# Was 4, which was a guess. On 2026-08-19 a real Ticketmaster checkout page
# was captured mid-hold with "11:39" on its countdown, so the true window is
# at least twelve minutes and probably a round fifteen. Kept deliberately
# short of what was observed: the number goes into an email telling him how
# long he has, and the error that costs a ticket is the optimistic one.
HOLD_MINUTES_HINT = int(os.environ.get("EP_HOLD_MINUTES_HINT", "10"))

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
    """Seconds between ticks of the watch loop.

    The loop no longer searches every page on every pass. Each page has its
    own interval and is searched when it comes due, so the tick is simply the
    shortest of them — anything slower would make the busiest page late, and
    anything faster would spend cycles with nothing to do.

    Request volume is therefore the sum of the per-page rates rather than a
    function of the cycle, which is what lets the pages be weighted by yield
    without spending more. See searches_per_hour().
    """
    # The SHORTEST gap any page can draw, not the mean. If the loop ticked at
    # the mean it could not honour a low draw: a page that drew 180s would not
    # be looked at until the next tick at 270s, and the bottom half of every
    # range would be silently unreachable.
    return min(e.fastest_gap_seconds for e in EVENTS) if EVENTS else _POLL_PER_EVENT_SECONDS


def searches_per_hour_at(hour: int) -> float:
    """Searches an hour across every page, at a given local hour.

    Now that the cadence has three windows, one number cannot describe it.
    This is the instantaneous rate — the one that has to stay under the
    ~20/hour that drew a block — and searches_per_day() is the one that says
    what the day actually costs.
    """
    from datetime import datetime

    when = datetime(2000, 1, 1, hour % 24, 30)
    total = 0.0
    for event in EVENTS:
        lo, hi = event.gap_range(when)
        if is_night(when) and NIGHT_POLL_SECONDS:
            lo = hi = max(NIGHT_POLL_SECONDS, lo)
        total += 3600.0 / ((lo + hi) / 2.0)
    return total


def peak_searches_per_hour() -> float:
    """The busiest hour of the day — the number that must stay under ~20."""
    return max(searches_per_hour_at(h) for h in range(24))


def searches_per_day() -> float:
    """What a full day actually costs, across all three windows."""
    return sum(searches_per_hour_at(h) for h in range(24))


def searches_per_hour() -> float:
    """Total searches an hour across every watched page.

    The number that actually has to stay under control — roughly 20 an hour is
    what got the home IP flagged in development — and the one to check after
    changing any page's interval.
    """
    return sum(e.searches_per_hour for e in EVENTS)


POLL_INTERVAL_SECONDS = poll_interval()

# ── Running more than one watcher ────────────────────────────────────────────
# A second watcher elsewhere doubles how often the page is looked at without
# either machine raising its own request rate. That is the only way to shorten
# the gap between looks without also shortening the gap between requests from
# one address — and with a mean listing life near 4.6 minutes against a
# 6-minute interval, the gap between looks is what decides whether a ticket is
# seen at all.
#
# The second one must not double the routine post. Set EP_ROLE=secondary and
# it reports on a much slower clock and stops narrating its own day/night
# switches, while every urgent alert — a listing, a basket, a broken watcher —
# still fires immediately from both. Silence being ambiguous is the thing this
# project refuses; two copies of "no luck yet" every hour is a different
# failure, where the alert that matters arrives in a stream nobody reads.
ROLE = os.environ.get("EP_ROLE", "primary").strip().lower()
IS_SECONDARY = ROLE == "secondary"

# Which watcher an alert came from, when there is more than one. Left unset on
# a single-machine setup, where the question does not arise.
WATCHER_LABEL = os.environ.get("EP_WATCHER_LABEL", "")

# Where this watcher sits in the polling cycle, as a fraction of one tick.
#
# For running a SECOND watcher somewhere else. Two independent watchers on
# different connections sample the page twice as often between them without
# either one raising its own request rate — which is the only way to shorten
# the gap between looks without also shortening the gap between requests from
# one address.
#
# It matters because of how short these listings are. Seven of eight distinct
# sightings were visible on exactly one poll, implying a mean life near 4.6
# minutes against a 6-minute interval — so roughly half of them come and go
# unseen. Two watchers make that roughly a quarter.
#
# Set EP_POLL_PHASE=0.5 on the second one and it starts half a tick out of
# step. Be honest about what this buys: the sleeps are jittered by ±25%, so
# the two drift out of step over hours. That costs less than it sounds —
# two independent samplers double the sampling rate whatever their phase, and
# the offset only stops them clumping together at the start.
POLL_PHASE = max(0.0, min(1.0, float(os.environ.get("EP_POLL_PHASE", "0"))))

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
#
# And the night theory turned out to be half the story. On 2026-08-18, after a
# power cut moved the watcher onto a mobile connection, two searches timed out
# at 11:14 and 11:17 — the first daytime timeouts ever recorded, against five
# that had all fallen between 22:08 and 00:59. The page is not only slow at
# night; it is slow over a slow link, and a tethered connection is one. Each
# timeout costs a resale-blind reading on both pages at once.
#
# So the daytime ceiling is raised to match the overnight one. The asymmetry
# argument above is the reason this is close to free rather than a trade: a
# healthy search returns the moment its marker appears, so the ceiling is only
# ever reached by a poll that was going to fail anyway. The extra wait is spent
# on failures, never on good polls. Two observations is thin evidence for the
# cause — but the cost of being wrong about it is 45 seconds of waiting, and
# the cost of leaving it is a listing that could not be seen.
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("EP_SEARCH_TIMEOUT", "90"))
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
#
# A secondary watcher reports far less often by default. Its job is to look
# more often, not to talk more often, and the primary is already proving
# hourly that the watch is alive.
HEARTBEAT_HOURS = float(
    os.environ.get("EP_HEARTBEAT_HOURS", "12" if IS_SECONDARY else "1")
)

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

# ── Naming the connections ───────────────────────────────────────────────────
# The watcher recognises any number of connections, not two. It identifies one
# by the default gateway's MAC address — see network.py for why the Wi-Fi SSID
# cannot be used — and learns each new one as the MacBook joins it.
#
# Naming is optional. An unnamed connection is still tracked, counted and
# blamed correctly; it is just described by its private range ("the
# 192.168.0.x network") instead of by a name. Two get guessed at: a gateway on
# 172.20.10.x or an iPhone USB port is called the hotspot, and the first
# connection the watcher ever sees is called home.
#
# To name one, put "key=Label" pairs here, comma separated. The key may be the
# gateway MAC, the gateway IP, or the public IP — whichever you have to hand;
# every "you are on a different connection" email prints the key to use.
#
#   EP_NETWORK_NAMES="9c:31:c3:93:d1:b1=home Wi-Fi,172.20.10.1=David's hotspot"
def _parse_network_names(raw: str) -> dict:
    names = {}
    for pair in (raw or "").split(","):
        key, sep, label = pair.partition("=")
        key, label = key.strip().lower(), label.strip()
        if sep and key and label:
            # MACs are normalised the same way network.gateway_mac() does, so
            # a name written with unpadded octets still matches.
            if key.count(":") == 5:
                key = ":".join(part.zfill(2) for part in key.split(":"))
            names[key] = label
    return names


NETWORK_NAMES = _parse_network_names(os.environ.get("EP_NETWORK_NAMES", ""))

# What to call the two the watcher can guess at.
HOME_NETWORK_LABEL = os.environ.get("EP_HOME_LABEL", "home Wi-Fi")
HOTSPOT_LABEL = os.environ.get("EP_HOTSPOT_LABEL", "David's hotspot")

# Optional, and now legacy: if set, a connection using this public IP is
# labelled home. Superseded by EP_NETWORK_NAMES, which is keyed on something
# that does not change every time a carrier re-addresses a tether — but it is
# still honoured, because it is set in the running deployment.
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
