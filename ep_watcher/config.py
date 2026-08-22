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
                 watch: bool = True, sweep: bool = True,
                 secure: bool = True, secure_priority: int = 0,
                 stop_after: str = ""):
        self.peak_min_seconds, self.peak_max_seconds = peak_min_seconds, peak_max_seconds
        self.offpeak_min_seconds = offpeak_min_seconds
        self.offpeak_max_seconds = offpeak_max_seconds
        #: Is this page searched at all?
        #:
        #: A step above `secure`, and the two are a ladder rather than a pair:
        #: False here means the page is never loaded, never searched, never
        #: swept and never alerted on, so `secure` below cannot arise. The
        #: page stays in EVENTS with its history, its cadence and its
        #: priority intact — nothing is deleted, it is switched off.
        #:
        #: That distinction is the whole point of the flag. `stop_after`
        #: retires a page for good on a date; this is the reversible version,
        #: for a page that is still wanted but not *now*, because its
        #: requests are needed somewhere more important. Switching it back on
        #: must be one edit and a restart, not an archaeology exercise.
        self.watch = watch
        #: Is this page included in the cheap resale sweep between searches?
        #:
        #: The middle rung of the ladder, and the only one that trades
        #: coverage for LATENCY rather than for silence. A page with
        #: sweep=False is still searched on its full cadence, still alerted
        #: on and still secured — it is simply not asked about in the ninety
        #: seconds between searches, so a listing on it is seen when the next
        #: search comes round instead of within the sweep interval.
        #:
        #: Separate from `watch` because the sweep is the only part of the
        #: watcher that has ever been rate-limited, and the refusals scale
        #: with the number of pages swept rather than with which ones. That
        #: makes "how many pages does the sweep cover" a dial worth having on
        #: its own, without switching a page off entirely to turn it.
        self.sweep = sweep
        #: May the buyer open a signed-in browser and hold this one?
        #:
        #: Per page, because "tell me about it" and "grab it for me" are not
        #: the same instruction, and a page may be worth watching without
        #: being worth an urgent walk to the laptop.
        #:
        #: Both weekend pages are True. The Early Entry Pass is governed by
        #: WATCH_EARLY_ENTRY below, which sets `watch` and `secure` together
        #: — see the comment there for why the two move as one.
        self.secure = secure
        #: Which page wins when two want the buying browser at once.
        #:
        #: Higher takes precedence, and it is a real precedence rather than a
        #: preference: a page that outranks a live hold will CLOSE that
        #: browser and take the ticket instead, dropping whatever was in the
        #: basket.
        #:
        #: David set the rule on 2026-08-19 — "weekend ticket is always
        #: priority, but try to get the early ticket as well". Both halves
        #: matter. The Early Entry Pass is still watched and still secured
        #: whenever the buying browser is free, because it is worth having;
        #: but Ticketmaster only honours it alongside a Weekend Ticket, so a
        #: held Early Entry pass while a weekend ticket goes by is the single
        #: worst outcome available — it spends the one browser on the one
        #: product that is useless on its own.
        #:
        #: The cost is stated plainly because it is real: preempting drops a
        #: hold that was certain for one that may already be gone. That is the
        #: trade his rule chooses, and it is the right way round, because a
        #: weekend ticket is the thing this project exists to find.
        self.secure_priority = secure_priority
        self.slug = slug
        self.name = name
        self.url = url
        #: Words that identify this event in the Discovery index, lowercase.
        self.match_words = tuple(w.lower() for w in match_words)
        #: The last date this page is worth searching, "YYYY-MM-DD", or ""
        #: to run until the watcher's own STOP_AFTER_DATE.
        #:
        #: Products on the same festival do not all stop being worth buying on
        #: the same day. The Early Entry Pass grants campsite access from 2pm
        #: on Thursday the 27th; from the 28th it is worth nothing, while the
        #: weekend tickets are still worth having. With only a global stop
        #: date the watcher spent a full day searching for an expired add-on —
        #: real requests against a rate limit that has already blocked this
        #: connection nineteen times — and, because securing is armed for that
        #: page, could have opened the buying browser for it.
        #:
        #: An expired page is dropped by due_events() rather than removed from
        #: EVENTS, so its history, its last reading and its place in the hourly
        #: report all survive. It stops being asked about; it does not stop
        #: having existed.
        self.stop_after = stop_after
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

    def expired(self, today: str = "") -> bool:
        """Has this page stopped being worth searching, whatever the watcher does?

        Compared as ISO date strings, the same way state.past_stop_date() does
        it, so there is no timezone argument about when a day ends.
        `stop_after` is the LAST day this page runs, so this is true from the
        following morning.
        """
        if not self.stop_after:
            return False
        from datetime import datetime, timezone

        now = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return now > self.stop_after

    def searchable(self, today: str = "") -> bool:
        """Should this page be searched right now, for any reason at all?

        The single predicate every caller asks, so that "switched off" and
        "past its date" cannot drift apart. Both mean the same thing to the
        request budget — no page load, no search, no sweep — and they were
        two separate checks for about an hour, which is exactly long enough
        for a new call site to remember one and forget the other.
        """
        return self.watch and not self.expired(today)

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

# 3-6 minutes at peak, restored on 2026-08-20 when the Early Entry Pass was
# switched off and gave its requests back.
#
# This page was on 3-6 until 2026-08-19, when Early Entry parity forced it out
# to 5-9 — three pages could not all be searched every few minutes under the
# ~20/hour ceiling, and the standard page paid for the pass. With the pass off
# there is nothing to pay, and leaving the page slow would mean the switch had
# bought nothing.
#
# Landed at 14.7 searches/hour rather than at the ceiling, deliberately. The
# watcher ran at 18.5/hour on the 19th and 20th and drew six blocks in two
# days; 20/hour is the documented line but the evidence says the high teens
# are already too warm. Every one of those blocks cleared on the first fresh
# profile — the identity ages out, not the address — so a block costs a poll
# cycle rather than the connection, which is why this is worth doing at all.
#
# What it buys: on a listing that has been live ~3.25 minutes on average
# before a search finds it, halving the mean gap from 7 minutes to 4.5 roughly
# halves that delay. Every securing attempt so far has arrived to find the
# listing gone, so the minutes before detection are the whole game.
#
# To undo: EP_STANDARD_PEAK_MIN=300 / EP_STANDARD_PEAK_MAX=540 puts it back.
STANDARD_PEAK_MIN_SECONDS = int(os.environ.get("EP_STANDARD_PEAK_MIN", "300"))
STANDARD_PEAK_MAX_SECONDS = int(os.environ.get("EP_STANDARD_PEAK_MAX", "560"))
STANDARD_OFFPEAK_MIN_SECONDS = int(os.environ.get("EP_STANDARD_OFFPEAK_MIN", "750"))
STANDARD_OFFPEAK_MAX_SECONDS = int(os.environ.get("EP_STANDARD_OFFPEAK_MAX", "1300"))

# The nominal range. Kept equal to the peak range by default so that
# poll_seconds — and therefore searches_per_hour() — describes the cadence
# actually in force. When these drifted apart on 2026-08-19 the standard page
# reported 13.3 searches an hour while really running at 8.6, which is exactly
# the kind of quiet lie the budget arithmetic exists to prevent.
STANDARD_POLL_MIN_SECONDS = int(
    os.environ.get("EP_STANDARD_POLL_MIN", str(STANDARD_PEAK_MIN_SECONDS)))
STANDARD_POLL_MAX_SECONDS = int(
    os.environ.get("EP_STANDARD_POLL_MAX", str(STANDARD_PEAK_MAX_SECONDS)))

# The Early Entry Pass — the cadence it will use WHEN IT IS SWITCHED BACK ON.
# Dormant today; see WATCH_EARLY_ENTRY.
#
# 15-30 minutes at peak, decoupled from the standard page on 2026-08-20. It
# was pinned to the standard page's range on the 19th, when David said the
# pass mattered as much as the weekend ticket. That instruction has been
# superseded: he switched the pass off entirely on the 20th because the
# weekend ticket is the critical thing and he does not have one, and the
# condition he set for turning it back on is having one.
#
# So the pass returns as what it will be on that day — a secondary page, for
# a thing he wants but has already got the important half of — and it returns
# on a secondary page's clock. This is not a demotion by opinion. It is what
# makes the switch safe to flip: if the pass still inherited the standard
# page's range, turning it on would add 13.3 searches an hour and take peak
# load to 28, well through the ~20/hour ceiling. The one switch he has been
# promised he can throw in a hurry must not be a switch that gets him blocked.
#
# The pass can afford it. These appear several times a day — five of the first
# eight finds recorded — and he needs exactly one, so a 15-30 minute clock
# still meets several a day. That is the opposite of the weekend ticket, where
# sightings are rare and the gap between looks is the whole game.
#
# On: 13.3 + 1.3 + 2.7 = 17.3/hour. Off: 14.7/hour. Both under the line, and
# tests/test_early_entry_switch.py asserts the first of those on every run.
EARLY_ENTRY_PEAK_MIN_SECONDS = int(
    os.environ.get("EP_EARLY_PEAK_MIN", "900"))
EARLY_ENTRY_PEAK_MAX_SECONDS = int(
    os.environ.get("EP_EARLY_PEAK_MAX", "1800"))
EARLY_ENTRY_OFFPEAK_MIN_SECONDS = int(
    os.environ.get("EP_EARLY_OFFPEAK_MIN", "1800"))
EARLY_ENTRY_OFFPEAK_MAX_SECONDS = int(
    os.environ.get("EP_EARLY_OFFPEAK_MAX", "3600"))

# The instalment plan, searched exactly as often as the standard page.
#
# It used to get a thirtieth of the attention: 30-60 minutes between searches
# against the standard page's 3-6, on the reasoning that one of nine sightings
# had ever appeared there. That reasoning judged the page by SUPPLY.
#
# What decides whether a page is winnable is how long a listing survives on
# it, and on 2026-08-21 that was measured across both event logs:
#
#     weekend-camping               median  2.1 min visible  (n=14)
#     early-entry                   median  7.3 min visible  (n=4)
#     weekend-camping-instalment            21.8 min visible (n=1)
#
# It is the same ticket at the same price, paid in stages, and a listing sat
# there untouched for twenty-two minutes — because hardly anyone thinks to
# watch it. Meanwhile five securing attempts on the standard page that day all
# reached a listing somebody else had already claimed, at attempt times from
# 6.6s to 82.5s with identical outcomes. Rare and winnable beats frequent and
# already gone.
#
# WHAT THIS COSTS, stated plainly because it crosses a line this project has
# been careful about. Two pages at 13.3/hour is 26.6/hour at peak, against the
# 20/hour that drew a 403 during development — about a third over. David asked
# for the two pages to be searched alike on 2026-08-21 with that number in
# front of him. EP_INSTALMENT_PEAK_MIN / _MAX are the dial if the blocks
# return; widening both pages to 240-480s would bring the pair back to 20/hour
# exactly, at the cost of a slower look at each.
#
# Note that the SWEEP already covers this page every ninety seconds and costs
# one XHR rather than a page load, so most of the detection gain was already
# in hand. These searches are what see primary stock, which the sweep cannot.
INSTALMENT_PEAK_MIN_SECONDS = int(
    os.environ.get("EP_INSTALMENT_PEAK_MIN", str(STANDARD_PEAK_MIN_SECONDS)))
INSTALMENT_PEAK_MAX_SECONDS = int(
    os.environ.get("EP_INSTALMENT_PEAK_MAX", str(STANDARD_PEAK_MAX_SECONDS)))
INSTALMENT_OFFPEAK_MIN_SECONDS = int(
    os.environ.get("EP_INSTALMENT_OFFPEAK_MIN", str(STANDARD_OFFPEAK_MIN_SECONDS)))
INSTALMENT_OFFPEAK_MAX_SECONDS = int(
    os.environ.get("EP_INSTALMENT_OFFPEAK_MAX", str(STANDARD_OFFPEAK_MAX_SECONDS)))


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
INSTALMENT_POLL_MIN_SECONDS = int(
    os.environ.get("EP_INSTALMENT_POLL_MIN", str(INSTALMENT_PEAK_MIN_SECONDS)))
INSTALMENT_POLL_MAX_SECONDS = int(
    os.environ.get("EP_INSTALMENT_POLL_MAX", str(INSTALMENT_PEAK_MAX_SECONDS)))

#: Kept as the mean of the standard range, for anything that still wants a
#: single number (the banner, and any page added without its own range).
STANDARD_POLL_SECONDS = (STANDARD_POLL_MIN_SECONDS + STANDARD_POLL_MAX_SECONDS) // 2
INSTALMENT_POLL_SECONDS = (INSTALMENT_POLL_MIN_SECONDS + INSTALMENT_POLL_MAX_SECONDS) // 2
DEFAULT_EVENT_POLL_SECONDS = STANDARD_POLL_SECONDS

#: Securing precedence, highest first. Only the ordering matters, not the
#: numbers; they are spaced so a page can be slotted between them later.
#:
#: BOTH weekend pages share this value, and that is deliberate rather than an
#: oversight — it was read as one on 2026-08-20 and is written down here so it
#: is not "fixed" later. Preemption requires strictly greater priority
#: (engine._maybe_secure), so equal values mean the two weekend pages cannot
#: take the buying browser from one another.
#:
#: That is the right way round. The instalment plan is the same weekend
#: camping ticket paid in stages, so a held instalment listing is not a
#: consolation prize, it is the thing this project exists to find. Letting the
#: standard page preempt it would drop a ticket that is already in a basket in
#: order to chase one that may have gone — trading a certainty for a maybe,
#: for no gain beyond a payment schedule.
#:
#: The add-on is the only case where preemption earns its cost, because an
#: Early Entry pass genuinely is worthless without a weekend ticket beside it.
SECURE_PRIORITY_WEEKEND = int(os.environ.get("EP_PRIORITY_WEEKEND", "100"))
SECURE_PRIORITY_ADDON = int(os.environ.get("EP_PRIORITY_ADDON", "10"))


# ── The Early Entry Pass: one switch, and this is it ─────────────────────────
#
#   OFF. To turn it back on:
#
#       echo 'export EP_EARLY_ENTRY=1' >> ~/.ep2026-watcher/env
#       ./restart.sh
#
#   That is the whole procedure. Nothing else has to change, and nothing
#   about the pass has been deleted — its page, its cadence, its priority,
#   its stop date and its history are all still here, waiting.
#
# David's instruction of 2026-08-20, and the reason is worth keeping because
# it tells you when to reverse it: the weekend ticket is the critical thing
# and he does not have one yet, so every request the watcher can spend should
# be spent looking for it. An Early Entry pass is not worth a single search
# until there is a ticket for it to sit beside — Ticketmaster's own note reads
# "Early Entry passes are only valid with a Weekend Ticket".
#
# So the trigger for flipping this back to 1 is a real weekend ticket in hand.
# At that moment the pass stops being a distraction and becomes the next thing
# worth catching, and it should be caught properly.
#
# WHY ONE FLAG AND NOT TWO. Searching and securing were separate settings, and
# the pass has held every combination of them over three days: watched and
# secured, watched and alert-only, and now neither. They are tied together
# here because both of the reasons for keeping them apart dissolve on the same
# day. Alert-only existed because each attempt spent a buying-browser cold
# start that a weekend ticket might need in the next minute; once a weekend
# ticket is bought, the buying browser has nothing else to do. Searching at
# all existed on the same budget the weekend pages were competing for; once
# they have stopped competing, the budget is free.
#
# The point is that "turn the Early Entry search back on" and "and actually
# get me one" have to be the same action. Turning on a search that only ever
# emails him, on the day he wants the pass held, would be a switch that looks
# like it worked and does half the job.
WATCH_EARLY_ENTRY = os.environ.get("EP_EARLY_ENTRY", "0").lower() in ("1", "true", "yes")

# Is the instalment-plan page included in the resale sweep?
#
#   echo 'export EP_SWEEP_INSTALMENT=0' >> ~/.ep2026-watcher/env
#   ./restart.sh
#
# ON, and it was briefly off on 2026-08-21 for a reason the same day's data
# then inverted. Both halves are recorded because the mistake is instructive.
#
# It was switched off on the argument that it had produced one find against
# the standard page's four, so dropping it halved the sweep's call rate for
# almost no loss of coverage. That treats a page's value as its supply.
#
# What actually matters is how long a listing SURVIVES there, because that is
# the window we have to win in. Measured over both event logs:
#
#     weekend-camping               median  2.1 min visible  (n=14)
#     early-entry                   median  7.3 min visible  (n=4)
#     weekend-camping-instalment            21.8 min visible (n=1)
#
# The instalment page sells the identical ticket at the identical price, paid
# in stages. When one appeared there it sat for twenty-two minutes. Against a
# ninety-second sweep that is not a race at all — it is the one page we can
# comfortably win, precisely BECAUSE hardly anyone knows to watch it.
#
# The standard page is the opposite. Five securing attempts on 2026-08-21 all
# reached a listing that had already been claimed by somebody else and was
# merely waiting to be paid for. We do not lose that page by being slow; we
# lose it because whoever is taking those tickets is not polling at all — the
# likeliest explanation is Ticketmaster's own ticket alerts, which push to a
# waiting list from inside their system with no latency for us to beat.
#
# So the sweep's scarce calls are better spent covering a page we can win than
# shaving seconds off one we cannot. n=1 on that 21.8 minutes, so this is the
# better bet rather than a certainty — but the cost of being wrong is one
# extra XHR every ninety seconds.
SWEEP_INSTALMENT = os.environ.get(
    "EP_SWEEP_INSTALMENT", "1").lower() in ("1", "true", "yes")


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
        secure_priority=SECURE_PRIORITY_WEEKEND,
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
        secure_priority=SECURE_PRIORITY_WEEKEND,
        # Searched and secured as always; swept only if asked. See
        # SWEEP_INSTALMENT above for the trade this represents.
        sweep=SWEEP_INSTALMENT,
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
    # Ticket".
    #
    # SWITCHED OFF as of 2026-08-20. Not searched, not swept, not alerted on,
    # not secured. See WATCH_EARLY_ENTRY above for the one line that reverses
    # that, and for why the trigger is a weekend ticket in hand.
    #
    # Everything below is left exactly as it was while the page was live —
    # the cadence, the priority, the stop date — because the flag is meant to
    # be flipped back and none of it is worth re-deriving on the day it is.
    # Deliberately NOT expressed as a commented-out event or a deleted one:
    # both would make turning it on an act of reconstruction, and this has to
    # be a switch.
    #
    # Note that the comment does not restate the flag's value. This exact
    # setting has held four positions in three days and the prose describing
    # it went stale twice, once badly enough to need its own commit. Read
    # WATCH_EARLY_ENTRY, or run `python -m ep_watcher budget`, which prints
    # what is actually in force.
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
        # Both from the one switch, so that turning the search back on also
        # turns the holding back on. See WATCH_EARLY_ENTRY.
        #
        # The four positions this has held, kept because the reasoning has
        # changed every time and the next change deserves to know:
        #
        #   * Watched, not secured (built): holding an add-on would pull him
        #     to a checkout for something useless without a weekend ticket.
        #   * Watched and secured (2026-08-19): "treat it as importantly as
        #     the ticket", with priority rather than exclusion keeping it safe.
        #   * Watched, not secured (2026-08-20 morning): passes at €46.50 turn
        #     out to appear several times a day — five of the first eight
        #     finds — and each attempt spends a buying-browser cold start.
        #   * Neither (2026-08-20 evening): the weekend ticket is critical and
        #     is not yet in hand, so the pass should not be spending searches
        #     that the weekend pages could use.
        #
        # secure_priority is kept and is not vestigial: when this comes back
        # on, the pass must still GIVE WAY to a weekend ticket rather than
        # outrank it. That stays true even in the case that switches it on —
        # a weekend ticket already bought — because the watcher has no way to
        # know the purchase happened, and would otherwise be one preemption
        # away from dropping a second real ticket for an add-on.
        watch=WATCH_EARLY_ENTRY,
        secure=WATCH_EARLY_ENTRY,
        secure_priority=SECURE_PRIORITY_ADDON,
        # Entry is from 2pm on the Thursday, so the 27th is the last day this
        # is worth anything at all. The weekend pages keep running to the
        # watcher's own stop date. See Event.stop_after.
        stop_after=os.environ.get("EP_EARLY_ENTRY_STOP_AFTER", "2026-08-27"),
    ),
]

def paused_pages() -> list:
    """Pages switched off on purpose, and still reversible. Never the expired.

    Kept apart from `expired()` deliberately. A page past its stop date is
    finished and saying so every hour would be nagging about the calendar; a
    page somebody switched off is a decision that can be un-made, and the
    watcher should keep saying it made it. The failure this guards against is
    the ordinary one — a switch flipped in an afternoon and forgotten for a
    week, while the thing it switched off is quietly assumed to be running.
    """
    return [e for e in EVENTS if not e.watch and not e.expired()]


def paused_note() -> str:
    """One line naming what is switched off, or "" when everything is on.

    Names the env var outright rather than pointing at the code, because the
    place this line is read is a phone. A pointer to a file on the MacBook is
    not an instruction anybody can act on from a train.

    EP_EARLY_ENTRY is the only such switch today and `watch` is only False on
    that one page. A second switchable page would need this to look the name
    up per event rather than assume it.
    """
    if not paused_pages():
        return ""
    names = ", ".join(e.name for e in paused_pages())
    return (f"NOT being searched: {names}. "
            f"Set EP_EARLY_ENTRY=1 in ~/.ep2026-watcher/env and restart to "
            f"turn it back on (it will be held as well as alerted on).")


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

# Quantities to search for, in order.
#
# ONE, by David's instruction, and it is not to be widened. This is also the
# most sensitive probe available: "there aren't enough tickets" is an answer
# about the number you asked for, so asking for the smallest number gives the
# earliest possible yes. The page defaults to 2, and searching for 2 when you
# would happily take 1 manufactures its own refusal against a listing that is
# really there. Anything that exists at all shows up at quantity 1.
#
# This block used to carry both instructions at once — an older paragraph
# arguing that sweeping several quantities is correct, followed immediately by
# the standing instruction to search for one. Two contradictory rationales in
# one comment is worse than either alone, because whichever a reader acts on
# they can cite the file for it.
#
# Sweeping remains possible for a one-off diagnostic (WANTED_QUANTITIES=1,2,3
# — each is a separate question whose answer does not imply the others), but
# every extra quantity is another real search against the rate limit, on a
# budget already close to the line that drew a block.
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
#: Where to send David to sign the buying profile in, most likely first.
#:
#: Tried in order until one loads, with the event page as a final fallback.
#: The exact path is not worth being confident about: `login-buy` opened the
#: event page until 2026-08-19, and he ran it and was never prompted for
#: anything, because Ticketmaster does not ask — the account control is an
#: icon that this project has already established is invisible to Playwright's
#: flattened text. Landing on a page with no visible next step is how a
#: one-command setup turns into a support conversation.
#: Credentials for the automated sign-in on the `automated-login` branch.
#:
#: From the environment ONLY, and only ever from ~/.ep2026-watcher/env, which
#: is chmod 600 and sourced by run_watcher.sh. Never a literal here, never in
#: a commit, never a command-line argument — an argument is visible in `ps`
#: to every process on the machine.
#:
#: The manual `login-buy` remains the default and needs none of this. David
#: asked for the automated path on 2026-08-19 for a secondary Yahoo account he
#: described as disposable, having been told that scripted sign-ins are what
#: account security is built to catch. That trade is his to make; it is the
#: reason this is opt-in and on its own branch rather than the default.
TM_EMAIL = os.environ.get("TM_EMAIL", "")
TM_PASSWORD = os.environ.get("TM_PASSWORD", "")


def have_login_credentials() -> bool:
    return bool(TM_EMAIL and TM_PASSWORD)


SIGNIN_URLS = tuple(
    u.strip() for u in os.environ.get(
        "EP_SIGNIN_URLS",
        # /member first because it redirects INTO the identity service with
        # the right client parameters attached. David's password manager has
        # the credential saved against auth.ticketmaster.com, which confirms
        # that is where the .ie site hands off to — but the OAuth entry point
        # cannot be constructed by hand without a real client_id, and an
        # invented one would burn an attempt on a 400. Let the site build it.
        "https://www.ticketmaster.ie/member,"
        "https://www.ticketmaster.ie/myacct,"
        "https://identity.ticketmaster.ie/sign-in",
    ).split(",") if u.strip()
)

BUY_PROFILE_DIR = Path(
    os.environ.get("EP_BUY_PROFILE_DIR",
                   Path.home() / ".ep2026-watcher" / "chrome-profile-buy")
)

# How long to keep trying to secure one listing before giving up and just
# alerting. Past this the listing is almost certainly in someone else's
# basket, and the honest thing is to tell him rather than keep clicking.
# Raised from 45 on 2026-08-19, when it was established that the buyer had
# never waited for the resale panel at all. Once it does wait, 45s is not
# enough to hold: the search alone can take 30s on a slow link, and the panel
# is a separate call after that.
#
# Spending longer is close to free. If the listing has gone, the extra seconds
# are spent by a browser that was going to fail anyway, and the availability
# alert has already been sent. If the listing is there, this is the whole
# game. The asymmetry says be patient.
#: Keep the signed-in buying browser open and parked, rather than launching
#: one when a listing appears.
#:
#: Measured on 2026-08-20: once detection lag was fixed, the whole remaining
#: gap was the ~60s between seeing a listing and clicking its row, and almost
#: all of that was a cold Chrome launch and the event page's 401-then-reload
#: dance — work that does not depend on the listing and can be done in
#: advance. The listings are consumed in well under a minute.
#:
#: The cost is a second Chrome resident all day and a signed-in session
#: sitting idle, which is one more thing for Ticketmaster to fingerprint.
#: That is a real trade and this switch exists so it can be reversed in one
#: environment variable.
WARM_BUY_BROWSER = os.environ.get("EP_WARM_BUY_BROWSER", "1").lower() in ("1", "true", "yes")

# Raised from 120 on 2026-08-21, because the first winnable chance this
# project has ever had was thrown away by this number.
#
# At 15:07 a listing on the instalment page was refused three times in a row
# while Ticketmaster's own feed kept returning THE SAME id — l1jwc8k6, never
# sold, still advertised — and the attempt stopped at 74.7s because the retry
# budget ran out, not because the ticket went. On a page whose listings
# survive a median 21.8 minutes, we walked away after one minute.
#
# WHAT THIS COSTS, because it is not free and the cost lands somewhere
# specific. BuyerWorker.submit() blocks the caller on done.wait(timeout_s),
# and the caller is the poll loop — so while a securing attempt is patient,
# the watcher is not polling or sweeping at all. At 300s plus the 60s margin
# secure_in_thread adds, that is a six-minute blind window, against a watchdog
# grace of 15 minutes. Safe from restart, but a real gap: another listing
# appearing in it is missed.
#
# Six minutes chasing a ticket the feed says is STILL THERE beats six minutes
# of looking for one that probably is not. That is the whole trade, and it is
# only ever spent on a listing already confirmed present — see secure(), which
# gives up at once when the feed agrees the ticket has gone.
SECURE_TIMEOUT_SECONDS = int(os.environ.get("EP_SECURE_TIMEOUT_SECONDS", "300"))

#: How many extra goes at a listing the feed says did not actually sell.
#:
#: Only ever spent on that one case. When Ticketmaster's dead-end screen and
#: its own resale feed agree the ticket is gone, it sold and there is nothing
#: to come back for — that returns immediately and costs no extra request.
#:
#: The case this exists for is the other one, and it is the one the evidence
#: keeps pointing at: the screen says sold, the feed still lists the ticket a
#: second later, and something is holding it rather than owning it. Baskets
#: lapse. Waiting one out is a race nobody else is running, whereas being
#: fractionally faster at the moment of refusal wins nothing, because at that
#: moment the ticket was not available to anybody.
#:
#: Three attempts at ~15s each with a pause between them fits inside
#: SECURE_TIMEOUT_SECONDS, which is what bounds the whole thing — the buying
#: browser cannot be held any longer than a single attempt could hold it
#: before, and a weekend ticket can still preempt the lot.
# Six, not two. Ticketmaster baskets hold for minutes, so two goes twenty
# seconds apart were never going to see one lapse — the mechanism existed but
# the numbers made it decorative. Bounded by SECURE_TIMEOUT_SECONDS above, so
# this is a ceiling on attempts rather than a promise of them; a slow
# connection will spend the budget on fewer, longer tries.
SECURE_RETRIES = int(os.environ.get("EP_SECURE_RETRIES", "6"))

#: Seconds between those goes.
#:
#: Long enough not to be a second refusal from the same wall, short enough to
#: fit several inside the window. Not tuned against evidence yet — no retry
#: has ever run — so it is a starting point that the `hold` records in the
#: event log will settle.
# Forty seconds between goes, up from twenty. Nothing observed suggests a
# basket lapses in twenty seconds — the one measured case was still held after
# seventy-five — so a short pause spent the budget on page loads instead of on
# waiting, which is the thing that actually has to happen.
SECURE_RETRY_PAUSE_SECONDS = float(os.environ.get("EP_SECURE_RETRY_PAUSE", "40"))

#: The shortest gap between two securing attempts on the same page.
#:
#: Securing used to be reachable only through the availability alert, so
#: "should David be told again" and "should we try again" were one decision
#: on one four-minute clock. On 2026-08-20 that cost a real chance: the 20:02
#: attempt lost, the sweep saw Weekend Camping stock again at 20:04 and 20:06,
#: and nothing was attempted either time, because the reading carried no edge
#: (resale already read AVAILABLE), no new listing by the description test,
#: and the re-nag had not elapsed. Four minutes of visible stock, one
#: thirteen-second attempt.
#:
#: They are separate questions now, on separate clocks. This is the securing
#: one, and it is deliberately much shorter: a repeat email is noise, but a
#: repeat attempt is the entire point of the machine. Sixty seconds is chosen
#: against the shape of an attempt rather than against politeness — an
#: attempt takes thirteen to seventeen seconds and the buying browser
#: serialises them anyway, so this bounds the page to one try a minute while
#: stock is visible without ever queueing them up.
SECURE_MIN_INTERVAL_SECONDS = float(
    os.environ.get("EP_SECURE_MIN_INTERVAL", "60"))

#: How many times to wait out a block screen before leaving it alone.
#:
#: Deliberately fewer, and slower, than an ordinary retry. Ticketmaster showed
#: the buying browser "Your Browsing Activity Has Been Paused" three times on
#: 2026-08-22 — the first evidence of the SIGNED-IN browser being challenged
#: rather than the watching one. A challenge is transient and worth waiting
#: out, but hammering one is precisely how a pause becomes a ban, and this
#: connection has already drawn twenty-two blocks.
#:
#: Two tries at sixty seconds, against six at forty for a basket. The
#: asymmetry is the point: a basket lapsing is something we are waiting FOR,
#: a challenge is something we caused.
SECURE_CHALLENGE_RETRIES = int(os.environ.get("EP_SECURE_CHALLENGE_RETRIES", "2"))
SECURE_CHALLENGE_PAUSE_SECONDS = float(
    os.environ.get("EP_SECURE_CHALLENGE_PAUSE", "60"))

#: How long to wait for the availability alert once the hold attempt is done.
#:
#: The alert is sent on its own thread so the buying browser does not queue
#: behind an SMTP handshake — see engine.handle(). This is the join at the end
#: of that, and it exists so the old guarantee survives: handle() does not
#: return until the alert has been attempted, so nothing about securing can
#: cost the one message this project exists to send.
#:
#: Generous, because by the time it is reached the hold attempt has already
#: run and the alert has had all of that time to finish. A send still going
#: after this is almost certainly a hung socket rather than a slow one, and
#: the thread is a daemon so abandoning it cannot wedge a restart.
ALERT_JOIN_SECONDS = float(os.environ.get("EP_ALERT_JOIN_SECONDS", "30"))

# ── Ringing David's phone ────────────────────────────────────────────────────
#
# Off unless all four are set, and off is the default. Email and push are the
# channels this project promises; a phone call is an extra, and an extra on
# the hottest path must never be able to break the thing that works.
#
# Why it is worth having: every other channel waits to be noticed, and these
# listings do not wait. The two weekend tickets on 2026-08-20 were gone inside
# a minute of appearing. A push seen ten minutes late is the same as no push,
# and a phone asleep on a table at 3am is the ordinary case rather than the
# unlucky one.
#
# Twilio because it rings a normal phone over the normal network: no app to
# install, nothing for iOS to kill overnight, and it works with the handset on
# silent — which is the state a phone is in for most of the hours this watcher
# is running. It costs about a euro a month for the number and roughly two
# cent a call.
#
# To switch on, in ~/.ep2026-watcher/env (chmod 600 — these are credentials):
#
#     export TWILIO_SID=AC...
#     export TWILIO_TOKEN=...
#     export TWILIO_FROM=+353...      # the Twilio number
#     export ALERT_PHONE=+353...      # David's mobile
#
# Then check it works, without waiting for a real ticket:
#
#     python -m ep_watcher ring
TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
ALERT_PHONE = os.environ.get("ALERT_PHONE", "")


def can_ring_phone() -> bool:
    """All four set, and the numbers shaped like numbers?

    Asked as a function so a test can set the values and reload. The format
    check is part of it because Twilio rejects a badly-formed number with an
    HTTP error rather than a ring, and the moment that would be discovered is
    the moment a ticket is on screen.
    """
    return bool(TWILIO_SID and TWILIO_TOKEN
                and phone_problem(TWILIO_FROM) is None
                and phone_problem(ALERT_PHONE) is None)


def phone_problem(number: str):
    """What is wrong with `number`, or None if it looks dialable.

    E.164 is what Twilio requires: a leading +, a country code, and digits.
    Checked here rather than trusted, because the two ways this goes wrong are
    both easy and both silent until it matters — a number copied from a
    contact card as "089 708 5212" has no country code and will never reach
    anybody, and one written "00353..." is the dialling prefix rather than the
    international form.

    Deliberately not a full validator. It is not this project's business to
    know the shape of every country's numbers; it is its business to catch the
    mistakes somebody actually makes at a keyboard.
    """
    if not number:
        return "not set"
    text = number.strip()
    if text.startswith("00"):
        return (f"{text} starts with the dialling prefix 00 — Twilio needs the "
                f"international form, so write it as +{text[2:]}")
    if not text.startswith("+"):
        return (f"{text} has no country code — it must start with + (an Irish "
                f"mobile is +353 followed by the number without its leading 0)")
    digits = text[1:]
    if not digits.isdigit():
        return f"{text} has something in it that is not a digit"
    if not 8 <= len(digits) <= 15:
        return (f"{text} has {len(digits)} digits, which is outside the 8-15 "
                f"that a real international number has")
    if text.startswith("+3530"):
        return (f"{text} keeps the 0 from the national form — an Irish mobile "
                f"written 089 708 5212 is +35389 7085212, with the 0 dropped")
    return None


#: Minutes before the phone may ring again.
#:
#: The availability alert re-fires every few minutes while a listing stays up
#: (see LIVE_RENAG_MINUTES), and being rung on every one of those is how
#: somebody learns to decline the call — which would cost exactly the ticket
#: this exists to catch. One ring, then the emails carry it.
RING_COOLDOWN_MINUTES = float(os.environ.get("EP_RING_COOLDOWN", "10"))

#: Short on purpose. The call is placed on the alert thread, and a slow
#: telephony API must not hold up the email and the push behind it.
RING_TIMEOUT_SECONDS = float(os.environ.get("EP_RING_TIMEOUT", "10"))

# How long David has once a ticket is held. Used only to word the alert,
# never to decide anything.
#
# Was 4, which was a guess. On 2026-08-19 a real Ticketmaster checkout page
# was captured mid-hold with "11:39" on its countdown, so the true window is
# at least twelve minutes and probably a round fifteen. Kept deliberately
# short of what was observed: the number goes into an email telling him how
# long he has, and the error that costs a ticket is the optimistic one.
HOLD_MINUTES_HINT = int(os.environ.get("EP_HOLD_MINUTES_HINT", "10"))

# Margin added to the hold window before the watcher stops protecting it.
#
# While a hold is live the watchdog is told not to restart the watcher, since
# restarting it kills the browser the basket lives in. That protection has to
# END, though, or a hold nobody completes would silence the watch for the rest
# of the fortnight — ambiguous silence being the one thing this project
# refuses. So it lasts as long as the hold plus this, and then the ordinary
# machinery resumes.
#
# Ten minutes on top of a ten-to-fifteen minute hold. Long enough to cover a
# walk to the laptop and a card number typed badly twice; short enough that a
# hold David never saw costs half an hour of watchdog cover rather than a day.
HOLD_PAUSE_EXTRA_MINUTES = float(os.environ.get("EP_HOLD_PAUSE_EXTRA", "10"))


def hold_window_minutes(measured=None) -> float:
    """How long to treat a live hold as live, in minutes.

    Prefers the countdown read off the checkout page over the configured
    estimate, for the same reason the alert does: the estimate comes from one
    observation of an entirely different event.
    """
    return float(measured or HOLD_MINUTES_HINT) + HOLD_PAUSE_EXTRA_MINUTES


# Set EP_USE_BROWSER=0 to run API-sources-only. That is the mode for anywhere
# without a real Chrome — GitHub Actions, a small VPS without a display — and
# it is much weaker: no primary ground truth and no per-listing resale. See
# the hosting section of the README before relying on it.
USE_BROWSER = os.environ.get("EP_USE_BROWSER", "1").lower() in ("1", "true", "yes")

# ── Runtime ──────────────────────────────────────────────────────────────────
REPO_DIR = Path(__file__).parent.parent

# Where this repo lives on the MacBook, as a string to print in instructions.
#
# REPO_DIR is wherever the code happens to be running, which is right for
# every message written on the Mac and wrong for the one message that is not.
# The "your Mac watcher has gone quiet" email is sent from a GitHub runner, so
# REPO_DIR there is the runner's checkout — and on 2026-08-19 David received
# an alert telling him to run
#
#     cd /home/runner/work/ep2026-ticket-watcher/ep2026-ticket-watcher
#
# on his laptop. That is the single alert that arrives when he is away from
# the machine and has to act on it from a phone, and its instructions pointed
# at a directory that exists only inside a container that had already been
# destroyed.
#
# So the Mac's path is stated rather than derived. Override with
# EP_MAC_REPO_DIR if the checkout ever moves.
MAC_REPO_DIR = os.environ.get(
    "EP_MAC_REPO_DIR", "~/SideProjects/EPTicketRefresher")

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

#: Photograph a securing attempt that failed.
#:
#: The find recorder dropped its screenshot in August for good reasons: it sat
#: on the critical path between a live listing and the click that might win
#: it, it only succeeded on 6 of 17 tries, and the JSON answered the question
#: anyway. None of that applies here. This fires only after an attempt has
#: already failed, so the ticket is lost and there is no clock left to
#: protect — and the question it answers, "what page was the browser actually
#: looking at", is one no JSON field had been recording at all.
HOLD_SCREENSHOTS = os.environ.get(
    "EP_HOLD_SCREENSHOTS", "1").lower() in ("1", "true", "yes")

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


#: Upper bound on the watch loop's tick, in seconds.
#:
#: The tick is how often the loop WAKES to ask "is any page due yet", not how
#: often it asks Ticketmaster anything. Finer means the drawn gaps are
#: honoured more precisely; it does not mean more requests, because a tick
#: with nothing due returns without opening a page.
#:
#: Lowered from 60 to 45 on 2026-08-20, when the standard page went back to a
#: 3-minute floor. The tick has to stay well under the shortest gap any page
#: can draw or it quantises the cadence upward: a page due at 180s is not
#: noticed until the next wake, so a 60s tick turns a configured 3-6 minutes
#: into an effective 4-7 and makes searches_per_hour() an overstatement. The
#: only cost of a finer tick is an idle pass through run_once().
LOOP_TICK_CEILING_SECONDS = int(os.environ.get("EP_LOOP_TICK_SECONDS", "45"))


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
    # Capped well below the shortest gap any page can draw, because the tick
    # is the resolution at which a due page can be noticed — not a rate at
    # which anything is requested.
    #
    # Ticking at the shortest gap sounds right and quantises badly. Measured
    # live on 2026-08-19 with a 300s tick against a 300-540s target: a page
    # that drew 516s comes due at 387s, the next tick lands at ~600s, and the
    # page is searched 10-12 minutes apart instead of 5-9. Observed gaps that
    # afternoon were 5, 11, 6, 7, 11, 8, 12 — a configured mean of 7 minutes
    # delivering nearly 9.
    #
    # A tick costs nothing on its own. When nothing is due, run_once() returns
    # an idle reading without opening a page or touching the network, so the
    # only price of a finer tick is a few CPU cycles. Request volume is set
    # entirely by the per-page gaps.
    fastest = min(e.fastest_gap_seconds for e in EVENTS) if EVENTS else _POLL_PER_EVENT_SECONDS
    return max(30, min(fastest, LOOP_TICK_CEILING_SECONDS))


#: The instantaneous request rate that actually drew a block, in searches per
#: hour, across all watched pages combined.
#:
#: Measured, not chosen. Polling every three minutes — roughly 20 searches an
#: hour — got this client answering HTTP 403 on 2026-08-13, from the same
#: headed Chrome that had been getting 200 all day, and the identical command
#: that had worked fifteen minutes earlier got 403 too. Every cadence decision
#: in this file is argued against that number.
#:
#: It is a constant here because until 2026-08-19 it existed only as the
#: phrase "~20/hour" repeated in a dozen comments, where nothing could check
#: it — and the comments had already drifted from the code they described,
#: variously claiming 15.3, 17 and 12 searches an hour for a configuration
#: really spending 18.5. Now `python -m ep_watcher budget` measures the live
#: settings against it and tests/test_request_budget.py fails the suite if a
#: cadence change crosses it.
#:
#: Treat it as a cliff with fog around it, not a fence. It is one observation
#: on one connection on one day; the real threshold is unpublished and may
#: move. Being under it has never been a guarantee — the watcher was blocked
#: again on 2026-08-19 at 05:43 while running below this rate.
BLOCK_RATE_PER_HOUR = float(os.environ.get("EP_BLOCK_RATE_PER_HOUR", "20"))


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
    # Only pages actually being searched. A page that is switched off or past
    # its stop date sends nothing, so counting it would overstate the rate —
    # and this number's whole job is to be the one that can be trusted against
    # the block line. Overstating is the safe direction for a limit, but it is
    # the unsafe direction for a decision: it would hide the headroom that
    # switching a page off is meant to buy, and the point of switching the
    # Early Entry Pass off is to spend that headroom on the weekend ticket.
    for event in (e for e in EVENTS if e.searchable()):
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

    Counts only the pages actually being searched, for the same reason
    searches_per_hour_at() does.
    """
    return sum(e.searches_per_hour for e in EVENTS if e.searchable())


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
#: Night ends at 06:00, not 07:00, because a real one appeared at 06:57.
#:
#: The weekend listing of 2026-08-21 was found at 05:57 UTC, which is 06:57
#: local — inside the old window, with the searches at half-hourly and only
#: the sweep looking. It is the single piece of evidence anyone has about
#: whether this market is awake before seven, and it says yes.
#:
#: The hour this gives back is off-peak (480-840s, so about five searches
#: rather than two), which is a small price for the hour immediately before
#: David wakes up — the one where a listing can sit unclaimed longest.
NIGHT_END_HOUR = int(os.environ.get("EP_NIGHT_END_HOUR", "6"))


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

# How often the backstop may repeat "your Mac watcher has gone quiet" about a
# silence it has already reported.
#
# It had no re-nag control at all, so it emailed and pushed once an hour for
# as long as the heartbeat was stale. On 2026-08-19 the heartbeat was stale
# because ntfy was rate-limiting the Mac, not because the Mac was down, and
# the prospect was an identical false alarm every hour until the quota reset —
# which is how an alert becomes something David swipes away without reading,
# including the time it is real.
#
# A silence is identified by WHEN the last beacon arrived, so a genuinely new
# outage still alerts immediately; only a repeat of the same unmoved silence
# is held back. Six hours matches the watchdog's own re-nag.
MAC_SILENT_RENAG_HOURS = float(os.environ.get("EP_MAC_SILENT_RENAG_HOURS", "6"))

# How often the Mac actually publishes that heartbeat.
#
# It used to publish on every handled reading — about 18 times an hour at the
# current cadence, roughly 450 a day, for a signal whose only consumer asks
# "was there one in the last 1.5 hours?". On 2026-08-19 that caught up with
# it: ntfy.sh answered HTTP 429 to the health check's own publish, and the
# most recent beacon was 76 minutes old against a 90-minute deadline. The
# dead man's switch was one slow poll away from declaring a perfectly healthy
# Mac dead.
#
# Worse than a false alarm, though, is what the quota is being spent ON.
# ntfy is the fast channel — the one that reaches a phone in seconds when a
# listing appears — and every beacon is a request that channel might have
# needed. Spending the budget proving the watcher is well, and running out
# when it has something to say, is precisely backwards.
#
# What actually happened, measured on 2026-08-19: 56 beacons went out at 3-4
# minute gaps, ntfy started answering 429, and NOTHING got through for the
# next 2.8 hours. The GitHub backstop — which judges the Mac by beacon age —
# duly emailed "your Mac watcher has gone quiet" about a watcher that was
# running perfectly and had just completed its 800th check.
#
# So the cost is not theoretical and it is not only waste. It was a false
# alarm, three hours of a disabled dead man's switch, and — because the beacon
# and the alert share one anonymous ntfy quota — three hours in which a real
# listing could not have reached the phone either. Email would still have
# worked; the fast channel would not.
#
# The limit being hit is the per-visitor daily message allowance rather than a
# burst limit, which is why it did not clear on its own: hammering it every
# three minutes for three hours kept it exhausted.
#
# Fifteen minutes is 96 beacons a day instead of ~450, which leaves the great
# majority of the allowance for messages that actually say something, and
# still fits six beacons inside every 1.5-hour silence window — five may fail
# before the switch has anything to complain about.
LIVENESS_INTERVAL_MINUTES = float(os.environ.get("EP_LIVENESS_MINUTES", "15"))

# How long to stop publishing entirely after ntfy answers 429.
#
# The throttle above stops the quota being exhausted; this stops it being held
# exhausted once it has been. A rate limiter refills over time and continuous
# requests keep the bucket empty, so a beacon that reacts to a refusal by
# trying again in three minutes is the reason a refusal lasts hours.
#
# Thirty minutes rather than sixty, and the reason is measured. During the
# 2026-08-19 block the Mac was refused for four hours straight — and yet a
# real alert push went through at 20:51, with probes on either side of it
# refused. So the bucket is not empty until some daily reset; it refills
# slowly and hands out the occasional token. Waiting a full hour would throw
# most of those away.
#
# The ceiling that matters is MAC_SILENT_HOURS: if the beacon cannot re-land
# within 90 minutes, the backstop calls the Mac dead. A 30-minute cooldown
# plus the 15-minute throttle gives two or three attempts inside that window
# instead of one, which is the difference between recovering quietly and
# sending David another false alarm.
LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES = float(
    os.environ.get("EP_LIVENESS_COOLDOWN", "30"))

# How many ntfy messages a day this machine may send, and how many of them to
# keep back for messages that actually say something.
#
# 250 is the anonymous allowance on ntfy.sh, confirmed the hard way on
# 2026-08-19: the server answered code 42908, "daily message quota reached",
# after the beacon had been publishing at 336 a day. Nothing counted, so the
# first anyone knew was a false "your Mac has gone quiet" five hours later —
# with the push channel dead throughout, which is the channel a ticket alert
# travels on.
#
# The reserve makes the beacon yield. When fewer than this many messages are
# left in the day, the heartbeat stops publishing and the remainder is kept
# for alerts: the same rule David set for tickets, that the important thing
# wins the scarce resource, applied to the other scarce resource here.
#
# A free ntfy.sh ACCOUNT raises the allowance well above this. That is the
# real fix and it needs David to create one; until then these numbers keep the
# watcher inside the anonymous limit with room to spare.
NTFY_DAILY_LIMIT = int(os.environ.get("EP_NTFY_DAILY_LIMIT", "250"))
NTFY_ALERT_RESERVE = int(os.environ.get("EP_NTFY_ALERT_RESERVE", "80"))

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
#: How often to ask the resale endpoint directly, between full searches.
#:
#: The whole reason this exists, measured on 2026-08-20. Weekend Camping was
#: searched 30 times that day at a mean gap of 6.5 minutes, so a listing had
#: already been live for ~3.25 minutes on average before the watcher saw it.
#: Every completed securing attempt — five of five — arrived to find the
#: listing gone, and the one at 11:48 went from detection to clicking the row
#: in under sixty seconds. The race is not being lost after we see a listing.
#: It is being lost before we see one.
#:
#: A full search is a page load, a quantity set, a button press and a wait for
#: the panel to paint. This is one same-origin XHR from the page that is
#: already open — the exact call the page makes for itself, whose own response
#: carries `cache-control: max-age=15`. Ticketmaster expects that endpoint to
#: be asked roughly every fifteen seconds; ninety is four times politer than
#: the page's own behaviour while cutting detection lag from minutes to under
#: a minute.
#:
#: It sees resale ONLY. Primary stock still needs the search, which keeps
#: running underneath on its own slower cadence — and resale is where all
#: eight finds to date have come from.
RESALE_SWEEP_SECONDS = int(os.environ.get("EP_RESALE_SWEEP_SECONDS", "90"))

#: Off switch, because a new source of requests against a rate limit that has
#: already blocked this connection twenty times deserves one.
RESALE_SWEEP = os.environ.get("EP_RESALE_SWEEP", "1").lower() in ("1", "true", "yes")

#: How many consecutive refusals send the sweep for a rest.
#: A sweep that is being refused is not finding tickets, it is only adding
#: evidence that this client is asking too often — the opposite of the job.
RESALE_SWEEP_MAX_REFUSALS = int(os.environ.get("EP_RESALE_SWEEP_MAX_REFUSALS", "3"))

#: How long that rest lasts before the sweep tries again, slower.
#:
#: This used to be forever — refusals ended the sweep for the life of the
#: process. On 2026-08-20 that happened twice in three hours, and both of the
#: real weekend listings found that day (17:42 and 18:13) were found by the
#: sweep rather than by a search. So the permanent stop switched off the
#: detector that works, for the rest of a run that lasts days, with no symptom
#: beyond finding nothing — and only a person noticing ever brought it back.
#:
#: The refusals look like a volume threshold rather than a verdict: the sweep
#: answers roughly sixty calls, is refused, and answers again after a rest.
#: Thirty minutes is a guess at "long enough to matter, short enough to leave
#: most of the day covered", and the sweep_backoff records in the event log
#: are what will settle it.
RESALE_SWEEP_BACKOFF_SECONDS = float(
    os.environ.get("EP_RESALE_SWEEP_BACKOFF", "1800"))

#: The slowest the sweep will go before it stops slowing down.
#:
#: Each rest doubles the interval, so a sweep that keeps being refused settles
#: at a rate the endpoint tolerates instead of oscillating between too fast
#: and off.
#:
#: This was 600s, justified as "still far faster than the searches". That was
#: simply wrong, and the night of 2026-08-20 proved it: the standard page's
#: peak search window is STANDARD_PEAK_MIN..MAX — 180 to 360 seconds, a mean
#: of 270 — so a sweep at ten minutes is SLOWER than the search it exists to
#: beat. A detector that has quietly become the slowest thing in the system is
#: worse than no detector, because the hourly numbers still report it working.
#:
#: 240 is the ceiling because it is below that 270-second mean: at its very
#: slowest the sweep is still, on average, ahead of the searches, which is the
#: minimum that makes it worth running at all. It is also 2.7x the base rate,
#: so it remains a real reduction in volume for an endpoint that has refused
#: us nineteen times.
RESALE_SWEEP_MAX_SECONDS = float(os.environ.get("EP_RESALE_SWEEP_MAX", "240"))

#: How many clean answers in a row earn the sweep its speed back.
#:
#: Backing off used to be one-way. `_interval` was set once at construction
#: and only ever doubled, so the sweep could get slower and never faster, and
#: the only thing that restored it was restarting the process.
#:
#: That is the same bug as the permanent stop above, one level down, and it
#: fired the same night. Three refusal bursts between 21:46 and 03:05 — at
#: hours when nothing is on sale and being slow costs nothing — walked the
#: interval 90 -> 180 -> 360 -> 600 and left it there. The weekend listing at
#: 05:57 was found by a sweep running every ten minutes, and the only reason
#: it was back at ninety seconds by morning is that the watchdog happened to
#: restart the watcher at 09:50 for an unrelated reason.
#:
#: So the ladder goes both ways. A run of answered calls halves the interval
#: back down, floored at RESALE_SWEEP_SECONDS. Twenty is roughly fifteen
#: minutes of clean sweeping at the base rate and forty at the ceiling: long
#: enough that it is not reacting to a single lucky call, short enough that a
#: quiet night cannot spend the following day.
RESALE_SWEEP_RECOVER_AFTER = int(
    os.environ.get("EP_RESALE_SWEEP_RECOVER_AFTER", "20"))

PROFILE_MAX_AGE_MINUTES = float(os.environ.get("EP_PROFILE_MAX_AGE", "90"))

# Hard floor on how often any single PAGE may be searched in press mode.
#
# There is a real tension here and it is worth stating rather than hiding. A
# resale listing observed during testing lived about five minutes, so a slow
# cadence genuinely misses tickets. But a fast cadence gets the client
# rate-limited, and a blocked watcher misses every ticket, not some of them.
# Two minutes is the floor because sustained polling faster than that is what
# produced the 403s; every page's range sits well above it.
#
# It measures the PAGE's gap, not the loop's tick, and the distinction became
# load-bearing on 2026-08-20. This floor was written when a tick WAS a search:
# the loop searched every page on every pass, so raising the tick genuinely
# slowed the requests. That stopped being true when pages got their own
# intervals — a tick now only searches pages that are due, and one with
# nothing due sends nothing at all. Applying the floor to the tick therefore
# stopped throttling requests and started coarsening the clock instead,
# stretching a configured 3-6 minutes to an effective 4-7 while the budget
# report went on quoting the configured figure.
#
# So it is checked against each page's shortest possible draw, which is the
# thing it was always meant to bound.
PRESS_MIN_INTERVAL_SECONDS = int(os.environ.get("EP_PRESS_MIN_SECONDS", "120"))
