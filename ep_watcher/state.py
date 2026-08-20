"""Run-to-run memory: what we last saw, and how the watcher itself is doing.

Kept as plain JSON with defaults filled in for any missing key, so adding a
field here never needs a migration and deleting state.json is always safe.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .model import GOOD_STATUSES, UNKNOWN


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: Optional[datetime] = None) -> str:
    return (dt or utc_now()).strftime("%Y-%m-%d %H:%M UTC")


def _defaults():
    return {
        "consecutive_failures": 0,
        "last_watchdog_alert": None,     # ISO8601, for the re-nag clock
        # Why the last failure happened, in one phrase, so the recovery email
        # can say what the outage was rather than only that it ended.
        "last_failure_reason": None,
        # When the current run of failures began, so the recovery email can
        # say how long the watcher was dark.
        "outage_started_at": None,        # ISO8601
        # The four availability fields that used to live here — last_primary,
        # last_resale, last_availability_alert, known_listings — moved into
        # state["events"][slug] when a second ticket page was added. They are
        # gone rather than kept as unused defaults: `status` printed them as
        # UNKNOWN forever, which reads as a watcher that has never seen a
        # thing, and a field nobody writes is a field that will eventually be
        # read by mistake.
        "last_success": None,             # ISO8601
        # Written on EVERY poll, success or failure. This is the liveness
        # signal: a hung process still counts as "running" to launchd, so a
        # wedged Chrome would never be restarted and the only symptom would
        # be silence. A timestamp that stops advancing is detectable.
        "last_check_at": None,            # ISO8601
        # Hourly "still nothing" report.
        "last_heartbeat": None,           # ISO8601
        "checks_since_heartbeat": 0,
        "failures_since_heartbeat": 0,
        # Coverage, as opposed to mere liveness. A poll can succeed and still
        # have learned nothing about resale — because the browser was blocked,
        # or because the resale panel never rendered. Counting those is the
        # difference between "the watcher is running" and "the watcher can
        # see", and only the second one catches a ticket.
        "degraded_since_heartbeat": 0,
        "degraded_total": 0,
        "resale_blind_since_heartbeat": 0,
        "resale_blind_total": 0,
        # Its own denominator, deliberately not checks_total. A state file
        # written before this was tracked has a large checks_total and a zero
        # blind count, which would render as a flawless 0% instead of "not
        # measured yet" — a health check that flatters itself on no evidence.
        "resale_checks_total": 0,
        # Connection health. Every HTTP 403 is recorded with a timestamp so
        # the emails can say whether this connection is in trouble, rather
        # than leaving David to infer it from a run of quiet failures.
        "block_history": [],              # ISO8601 timestamps, pruned to 7 days
        "checks_total": 0,
        # Alternating home Wi-Fi / phone hotspot.
        "current_ip": None,
        # Which connection, as opposed to which address. Keyed on the default
        # gateway's MAC, so a carrier re-addressing a tether is no longer
        # mistaken for joining a different network. See network.fingerprint().
        "current_net": None,
        "current_ip_since": None,         # ISO8601
        "searches_on_current_ip": 0,
        "networks": {},                   # ip -> {first_seen, searches, blocks}
        "rotation_asked_at": None,        # ISO8601, so we don't nag every hour
        # Last "you are on a different connection" email, so a carrier
        # re-addressing a tether cannot fill the inbox.
        "last_network_email_at": None,    # ISO8601
        "stop_notified": False,           # the final "watcher stopped" email
        # When a deliberate 403 backoff is due to end. While this is in the
        # future the watcher is *supposed* to be idle, and neither the
        # watchdog nor doctor should treat the still timestamp as a hang.
        "backoff_until": None,            # ISO8601
        # When a live basket is expected to expire. While this is in the
        # future the watcher is holding a ticket and MUST NOT be restarted:
        # the basket lives in the browser the watcher launched, so killing
        # the process throws the ticket away. See note_hold().
        "hold_until": None,               # ISO8601
        # Which page the live hold belongs to, and how important it is. A
        # weekend ticket is allowed to close the browser on a held Early Entry
        # pass, so the decision needs to know what is in there.
        "hold_event_slug": None,
        "hold_priority": 0,
        # When ntfy last refused this client, and until when to stop asking.
        # Persisted rather than kept in the process, because a restart used to
        # reset the cooldown and immediately fire another request into an
        # endpoint that was still refusing — which is what holds a rate
        # limiter empty. Two restarts on 2026-08-19 cost two such requests.
        "ntfy_cooldown_until": None,      # ISO8601
        # The backstop's own re-nag control: which silence it last alerted
        # about, and when. Without these it emailed "your Mac has gone quiet"
        # every hour about the same unmoved heartbeat.
        "mac_silent_alerted_at": None,    # ISO8601
        "mac_silent_beacon_at": None,     # ISO8601 — identifies WHICH silence
        # When the browser profile was last thrown away and rebuilt. The
        # bot-check cookies age out, so this drives a pre-emptive refresh —
        # see config.PROFILE_MAX_AGE_MINUTES.
        "profile_reset_at": None,         # ISO8601
        # When the next poll is due. Lets the watchdog judge lateness against
        # the cadence actually in force — which changes overnight — instead
        # of a fixed threshold that only ever matched the daytime one.
        "next_poll_due": None,            # ISO8601
        # When the runtime directory was last copied. Daily rather than on
        # every poll: the things worth saving change slowly, and the browser
        # session being copied is several megabytes.
        "last_backup_at": None,           # ISO8601
    }


def load() -> dict:
    """Read the state file, filling in defaults for anything missing.

    A missing file is ordinary — a first run, or a deliberate reset — and is
    silent. A file that EXISTS and cannot be parsed is not ordinary and says
    so, because it means the watcher has just forgotten everything it knew:
    which listings it has already alerted on, how many blocks this connection
    has drawn, and whether a ticket is currently held. That last one matters
    most, and losing it silently is how a corrupt file becomes a lost ticket.
    """
    state = _defaults()
    try:
        with open(config.STATE_FILE, "r") as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[{stamp()}] WARNING: state file unreadable ({exc}) — "
              f"starting from defaults. Everything the watcher remembered is gone.")
    return state


def save(state: dict) -> None:
    """Write the state file atomically.

    A plain `open(path, "w")` truncates the real file before a single byte of
    the new content is written, so a process killed inside that window leaves
    an empty or half-written state.json — and load() then quietly starts over
    from defaults.

    That window is not theoretical here. The watchdog's repair is
    `launchctl kickstart -k`, which is a kill, and this file is written on
    every cycle and every thirty seconds while a ticket is held. Among the
    things that would be lost is `hold_until` — the marker that stops the
    watchdog killing a live checkout — which makes the failure feed itself:
    the kill destroys the evidence that the kill was the wrong thing to do.

    Writing beside the file and renaming over it makes the swap atomic. A
    reader — the watcher, the watchdog, `doctor` — sees either the whole old
    file or the whole new one, never a fragment. The fsync is what makes that
    true across a power cut as well as a kill, which this project has already
    had one of.
    """
    tmp = None
    try:
        config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.STATE_FILE.parent / (config.STATE_FILE.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.STATE_FILE)
    except Exception as exc:
        # Deliberately every exception, not just OSError.
        #
        # run_once() saves in a `finally`, so anything raised here escapes
        # from the middle of a poll and — because it is a finally — would
        # mask whatever the poll was really doing. OSError covers the disk;
        # it does not cover a value that will not serialise, which is a
        # TypeError from json.dump and exactly the kind of thing a new field
        # introduces. The watch loop would then catch it upstairs, decide the
        # browser was at fault, and cold-restart Chrome once per cycle
        # forever, chasing a fault in a file.
        #
        # Failing to write the state costs the memory of one poll. Raising
        # costs the poll itself, and possibly every poll after it.
        print(f"[{stamp()}] WARNING: could not save state: {exc}")
        # Do not leave a half-written temp file behind to be mistaken for
        # anything, or to fail the next rename on a full disk.
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def minutes_since(iso: Optional[str]) -> float:
    """Minutes since an ISO timestamp, or 0.0 if it is missing or unreadable."""
    hours = _hours_since(iso)
    return 0.0 if hours is None else hours * 60.0


def _hours_since(iso: Optional[str]) -> Optional[float]:
    dt = _parse(iso)
    if dt is None:
        return None
    return (utc_now() - dt).total_seconds() / 3600.0


# ── Alert gating ─────────────────────────────────────────────────────────────

def event_state(state: dict, slug: str) -> dict:
    """This event's own availability history, created on first sight.

    Availability is tracked per event and everything else — failures, blocks,
    networks, heartbeat — stays global, because those describe the watcher
    rather than any one ticket page. Sharing availability across events would
    mean a listing on one updating the "last seen" values for the other and
    silencing its alert entirely.
    """
    slug = slug or "default"
    events = state.setdefault("events", {})
    return events.setdefault(slug, {
        "last_primary": UNKNOWN,
        "last_resale": UNKNOWN,
        "last_availability_alert": None,
        "known_listings": [],
        # When this page was last actually searched. Pages no longer share a
        # cycle — each has its own interval, weighted by how often a listing
        # has ever appeared on it — so the watcher has to remember when it
        # last looked at each one.
        "last_polled_at": None,
        # Failures are counted per event too, and that is not a detail.
        # A single shared counter meant a healthy page reset the count a
        # broken one had just incremented — so one page could fail on every
        # cycle for a fortnight while the watchdog never fired and the hourly
        # email reported everything fine. Reproduced before it was fixed.
        "consecutive_failures": 0,
    })


#: How much early a page may be searched, as a fraction of its interval. The
#: loop's sleep is jittered by ±25%, so without this a tick that landed a few
#: seconds short would skip the page entirely and double its real interval.
DUE_TOLERANCE = 0.75


def note_event_polled(state: dict, slug: str, gap_seconds: int = 0) -> None:
    """Record that this page was just searched, and when it is next due.

    `gap_seconds` is drawn once, HERE, and stored — not recomputed while
    waiting. Drawing it on each tick instead would collapse the configured
    range to its floor, because the page would become due the first time any
    draw landed low. Storing it also means the gap survives a restart, so a
    watcher that is restarted every few minutes cannot poll continuously.
    """
    ev = event_state(state, slug)
    ev["last_polled_at"] = utc_now().isoformat()
    if gap_seconds:
        ev["next_gap_seconds"] = int(gap_seconds)


def minutes_since_event_poll(state: dict, slug: str):
    """How long since this page was last searched, or None if never."""
    hours = _hours_since(event_state(state, slug).get("last_polled_at"))
    return None if hours is None else hours * 60.0


def event_gap_seconds(state: dict, event) -> int:
    """The gap this page is currently waiting out, in seconds.

    The stored draw when there is one; otherwise a fresh draw which is then
    STORED, so every later question about this page gets the same answer. The
    fallback covers a state file written before ranges existed and a page
    added while the watcher is running.

    Persisting the fallback is not tidiness. event_due() is asked about the
    same page several times in a tick, and a fallback that re-drew each time
    made it answer differently within one cycle — a page could be "not due"
    for the loop and "due" for anything that asked afterwards. It also made
    the effective interval the minimum of many draws rather than a sample
    from the range, which is the exact failure this whole design avoids.
    """
    ev = event_state(state, event.slug)
    stored = ev.get("next_gap_seconds")
    if isinstance(stored, (int, float)) and stored > 0:
        return int(stored)
    drawn = event.next_gap()
    ev["next_gap_seconds"] = int(drawn)
    return int(drawn)


def event_due(state: dict, event) -> bool:
    """Is this page due a search?

    A page that has never been searched is always due, so a fresh state file
    or a newly added page is read on the first tick rather than waiting out
    its interval.

    Measured against the gap drawn when the page was last searched, so the
    randomised cadence is honoured rather than re-rolled — see
    note_event_polled().
    """
    since = minutes_since_event_poll(state, event.slug)
    if since is None:
        return True
    return since * 60.0 >= event_gap_seconds(state, event) * DUE_TOLERANCE


def due_events(state: dict, events) -> list:
    """Which pages to search on this tick.

    May legitimately return nothing, and that is the point. Each page waits
    out a gap drawn from its own range, and the watch loop ticks faster than
    the shortest of those gaps so it can honour a low draw. A tick that finds
    nothing due must therefore do nothing and sleep again.

    This used to force the longest-waiting page instead of returning empty,
    on the grounds that a tick which polls nothing is a silent no-op. That was
    right while the tick equalled the busiest page's interval, so something
    was always due. Once the gaps became ranges on 2026-08-19 it would have
    defeated the whole mechanism — the forced poll would fire on nearly every
    tick and the page would be searched at the tick rate, not at its own.

    The stall guard below keeps the original protection: a page that is more
    than twice its longest gap overdue is searched regardless, so a clock
    jumping forward or a corrupted timestamp cannot park a page for ever.
    """
    # A page past its own stop date is not due, and cannot be rescued by the
    # stall guard either — "overdue" is meaningless for something nobody
    # should be asking about any more. Filtered first so both paths below
    # inherit it. See Event.stop_after: the Early Entry Pass is worthless from
    # the 28th while the weekend tickets still matter, and searching for it
    # spends real requests against a rate limit that has already blocked this
    # connection nineteen times.
    live = [e for e in events if not e.expired()]
    due = [e for e in live if event_due(state, e)]
    if due:
        return due
    stalled = [
        e for e in live
        if (minutes_since_event_poll(state, e.slug) or 0.0) * 60.0
        > max(e.poll_max_seconds, e.poll_seconds) * 2
    ]
    return stalled


def event_summaries(state: dict) -> list:
    """[(name, url, primary, resale)] for every watched page, from its history.

    Lets the hourly report speak about all the pages rather than about
    whichever single reading happened to trip the heartbeat clock. The old
    report printed that one reading beneath a link hardcoded to the first
    event, so it could show the instalment plan's statuses under the standard
    page's URL with nothing to signal the mismatch.
    """
    out = []
    for event in config.EVENTS:
        ev = event_state(state, event.slug)
        out.append((
            event.name,
            event.url,
            ev.get("last_primary", UNKNOWN),
            ev.get("last_resale", UNKNOWN),
            # How old this reading is. Pages are searched at different rates
            # now, so two statuses printed side by side may be minutes and
            # half an hour old respectively — and without saying so, the
            # older one reads as being just as fresh as the newer.
            minutes_since_event_poll(state, event.slug),
        ))
    return out


def worst_event(state: dict) -> tuple:
    """(slug, failures) for whichever event is doing worst. ("", 0) if all fine."""
    worst_slug, worst_count = "", 0
    for slug, ev in state.get("events", {}).items():
        count = ev.get("consecutive_failures", 0)
        if count > worst_count:
            worst_slug, worst_count = slug, count
    return worst_slug, worst_count


def _sync_global_failures(state: dict) -> None:
    """Global counter = the worst event's.

    Kept so everything that reads state["consecutive_failures"] — the
    watchdog gate, the emails, doctor — keeps working unchanged, while no
    longer being resettable by a *different* event succeeding.
    """
    _, worst = worst_event(state)
    state["consecutive_failures"] = worst


def should_alert_availability(state: dict, reading, new_listings=()) -> tuple:
    """Decide whether this reading deserves an email, and why.

    MUST be called before record_success(), which overwrites the very fields
    this compares against. Getting that order wrong silently disables all the
    edge detection below, leaving only the periodic re-nag — so a ticket that
    appeared, sold, and appeared again inside the re-nag window would produce
    no second alert at all.

    Edge-triggered on each market independently, so resale appearing still
    alerts even on a run where primary was already available (and vice versa).
    A newly-seen listing also counts: a flat per-market boolean hides
    tier-level changes, where one listing sells and another appears while the
    market never stops reading "available".

    Then it re-nags on a slow clock while the good state persists — one missed
    push notification shouldn't cost the ticket, but neither should a stuck
    'available' spam the inbox every minute for a day.
    """
    ev = event_state(state, getattr(reading, "event_slug", ""))
    reasons = []
    if reading.primary in GOOD_STATUSES and ev["last_primary"] not in GOOD_STATUSES:
        reasons.append(f"primary stock went {ev['last_primary']} → {reading.primary}")
    if reading.resale in GOOD_STATUSES and ev["last_resale"] not in GOOD_STATUSES:
        reasons.append(f"resale went {ev['last_resale']} → {reading.resale}")
    if new_listings:
        reasons.append(f"new listing(s): {', '.join(new_listings)}")

    if reasons:
        return True, "; ".join(reasons)

    if reading.any_good:
        # Minutes, not hours, while something is actually live. These listings
        # last ten to twenty minutes; an hourly reminder would arrive after the
        # ticket had gone, which makes it a reminder about nothing.
        since = _hours_since(ev["last_availability_alert"])
        if since is None or since * 60 >= config.LIVE_RENAG_MINUTES:
            return True, (
                f"still available — reminder ({config.LIVE_RENAG_MINUTES:.0f} min)"
            )

    return False, ""


def should_alert_watchdog(state: dict) -> bool:
    """True if the watcher is broken enough, and quiet for long enough, to nag.

    Unlike the previous latch-once design, this keeps nagging every
    WATCHDOG_RENAG_HOURS for as long as the thing is broken.
    """
    if state["consecutive_failures"] < config.WATCHDOG_FAILURE_THRESHOLD:
        return False
    since = _hours_since(state["last_watchdog_alert"])
    return since is None or since >= config.WATCHDOG_RENAG_HOURS


def pending_listings(state: dict, reading) -> list:
    """Listings in this reading that weren't in the last one. Does not mutate.

    Separate from record_success on purpose: the alerting decision needs to
    see this *before* state is updated, and folding the two together is what
    made the edge detection silently useless.
    """
    ev = event_state(state, getattr(reading, "event_slug", ""))
    previous = set(ev.get("known_listings", []))
    return [l.describe() for l in reading.listings if l.describe() not in previous]


def record_success(state: dict, reading, healthy: bool = True) -> list:
    """Fold a good reading into state. Returns newly-seen listing descriptions.

    Call this AFTER should_alert_availability() — it overwrites the fields
    that decision compares against.

    `healthy=False` is the partial case: a source failed but another answered.
    The data is real and is kept, but the run must not clear the failure
    counter, or a browser blocked for hours never escalates to the watchdog
    because every poll keeps resetting it to zero.
    """
    new = pending_listings(state, reading)
    ev_fail = event_state(state, getattr(reading, "event_slug", ""))
    if healthy:
        ev_fail["consecutive_failures"] = 0
    else:
        ev_fail["consecutive_failures"] = ev_fail.get("consecutive_failures", 0) + 1
    _sync_global_failures(state)
    state["last_success"] = utc_now().isoformat()

    # Per event, matching should_alert_availability() and pending_listings().
    # These three have to agree about where availability history lives: while
    # the reads moved here and this write did not, every second sighting of
    # the same listing looked new and re-alerted.
    ev = event_state(state, getattr(reading, "event_slug", ""))
    ev["known_listings"] = [l.describe() for l in reading.listings]
    ev["last_primary"] = reading.primary
    ev["last_resale"] = reading.resale
    return new


def record_failure(state: dict, reading=None) -> int:
    """Count a failed read against the event it happened to.

    Returns the worst event's streak, which is what the watchdog gates on —
    so one page failing forever escalates even while the other is fine.
    """
    if reading is None:
        # No event context (older callers, and the single-event tests).
        state["consecutive_failures"] += 1
        return state["consecutive_failures"]

    ev = event_state(state, getattr(reading, "event_slug", ""))
    ev["consecutive_failures"] = ev.get("consecutive_failures", 0) + 1
    _sync_global_failures(state)
    return state["consecutive_failures"]


# ── Hourly heartbeat ─────────────────────────────────────────────────────────

def past_stop_date() -> bool:
    """Has the watcher outlived the event it was built for?

    Compared as ISO date strings, which sort correctly and avoid any timezone
    argument about when exactly a day ends. STOP_AFTER_DATE is the last day
    the watcher runs, so this is true from the following morning.
    """
    if not config.STOP_AFTER_DATE:
        return False
    return utc_now().strftime("%Y-%m-%d") > config.STOP_AFTER_DATE


def should_send_heartbeat(state: dict) -> bool:
    """True once the heartbeat interval has elapsed with no ticket found.

    On the very first run there is no previous heartbeat, so the clock starts
    rather than firing immediately — otherwise starting the watcher would
    always send a "no success in the last hour" email covering no time at all.
    """
    since = _hours_since(state["last_heartbeat"])
    if since is None:
        return False
    return since >= config.HEARTBEAT_HOURS


def start_heartbeat_clock(state: dict) -> None:
    """Begin the countdown without sending anything. Used on first run."""
    if state["last_heartbeat"] is None:
        state["last_heartbeat"] = utc_now().isoformat()


def note_check(state: dict, unhealthy: bool) -> None:
    """Count a poll. `unhealthy` covers both total and partial failure.

    Named for what it means rather than for `reading.failed`: a poll where
    the browser was blocked but the free API answered is not a clean check,
    and counting it as one is what made the hourly email report "0 failed"
    through a real outage.
    """
    state["checks_since_heartbeat"] += 1
    if unhealthy:
        state["failures_since_heartbeat"] += 1


def note_degraded(state: dict, sources) -> None:
    """Record that the poll answered, but not from everything that should have."""
    state["degraded_since_heartbeat"] = state.get("degraded_since_heartbeat", 0) + 1
    state["degraded_total"] = state.get("degraded_total", 0) + 1


def note_resale_visibility(state: dict, reading) -> None:
    """Record whether anything could actually read resale on this poll.

    Tracked separately from failure because the two come apart. A poll can
    succeed, report a confident UNAVAILABLE on primary, and still have no
    idea about resale — the search resolved before the resale panel arrived.
    Measured over the first day of running, that was about one poll in six,
    and nothing surfaced it. Resale is the market a ticket has actually
    appeared on, so being blind to it is the failure that matters most.
    """
    state["resale_checks_total"] = state.get("resale_checks_total", 0) + 1
    if reading.resale == UNKNOWN:
        state["resale_blind_since_heartbeat"] = (
            state.get("resale_blind_since_heartbeat", 0) + 1
        )
        state["resale_blind_total"] = state.get("resale_blind_total", 0) + 1


def coverage(state: dict) -> tuple:
    """(degraded, resale_blind) polls since the last heartbeat."""
    return (
        state.get("degraded_since_heartbeat", 0),
        state.get("resale_blind_since_heartbeat", 0),
    )


#: Polls needed before a resale-visibility percentage means anything. Twelve
#: is about an hour at the current cadence — long enough to include a slow
#: cold start without being dominated by it.
MIN_RESALE_SAMPLE = 12


def resale_visibility(state: dict) -> tuple:
    """Return (severity, headline) for how often resale can actually be read.

    Answers the question the rest of the health checks do not: not "is the
    watcher running" but "can it see the market a ticket appears on". Over
    the first day of running that was about one poll in six, and nothing
    reported it.
    """
    total = state.get("resale_checks_total", 0)
    blind = state.get("resale_blind_total", 0)
    if not total:
        return "unknown", "not measured yet — starts on the next poll"

    # A rate needs a denominator worth dividing by. Straight after a fresh
    # start there are two polls, and the first one spends its time clearing
    # the cookie dialog and the bot check — so a single slow panel reads as
    # 50% blind and reports FAIL for a watcher that is working perfectly.
    # Crying wolf on two data points is how a health check gets ignored.
    if total < MIN_RESALE_SAMPLE:
        return (
            "unknown",
            f"only {total} poll(s) so far — too few to judge "
            f"(needs {MIN_RESALE_SAMPLE})",
        )

    # Thresholds set against what a blind poll costs rather than against a
    # tidy round number. A resale listing on this event was observed living
    # about five minutes — roughly one poll interval — so each blind poll is
    # very nearly one missed chance, not a fractional one. One in ten is
    # already worth saying out loud; one in three is a watcher that is mostly
    # not doing its job.
    pct = 100.0 * blind / total
    headline = f"resale readable on {total - blind}/{total} polls ({100 - pct:.0f}%)"
    if pct >= 33:
        return "bad", headline + " — blind on a third of polls or more"
    if pct >= 10:
        return "watch", headline + " — each blind poll is a listing that could be missed"
    return "ok", headline


def reset_heartbeat(state: dict) -> None:
    state["last_heartbeat"] = utc_now().isoformat()
    state["checks_since_heartbeat"] = 0
    state["failures_since_heartbeat"] = 0
    state["degraded_since_heartbeat"] = 0
    state["resale_blind_since_heartbeat"] = 0


def hours_since_heartbeat(state: dict):
    return _hours_since(state["last_heartbeat"])


def note_backoff(state: dict, seconds: float) -> None:
    """Record that the watcher is deliberately idle until `seconds` from now.

    A watcher backing off from a 403 and a watcher wedged on a hung Chrome
    look identical from outside: in both cases last_check_at stops advancing.
    But the correct response is opposite — one must be left alone, the other
    must be restarted — so the difference has to be written down rather than
    inferred.

    The consequence of getting it wrong is not cosmetic. The backoff doubles,
    30 minutes to a 3-hour cap, and past 45 minutes the watchdog would start
    restarting a watcher that is deliberately resting. Each restart polls
    immediately, against the connection that is already rate-limited, at
    3am with nobody awake — turning a short block into a long one and
    defeating the exact mechanism meant to protect the IP needed for buying.
    """
    state["backoff_until"] = (utc_now() + timedelta(seconds=seconds)).isoformat()


def clear_backoff(state: dict) -> None:
    state["backoff_until"] = None


def note_hold(state: dict, minutes: float, event_slug: str = None,
              priority: int = 0) -> None:
    """Record that a ticket is held in a browser, until `minutes` from now.

    This exists because the two halves of the project were about to destroy
    each other. The watchdog restarts the watcher when its poll clock stops
    advancing, which is right in every case but one: when the watcher has
    stopped on purpose because it is holding a basket. On the primary-stock
    path it did exactly that — printed "Reserve accepted — pausing the loop so
    you can check out", then slept forever without writing anything down. The
    poll became overdue, the watchdog ran its fifteen-minute check, and
    `launchctl kickstart -k` killed the process. The basket lives in the
    browser that process launched, so the ticket went with it.

    That is the worst failure this codebase can produce. Every other bug
    costs a ticket that was never in hand; this one throws away a ticket
    already caught, silently, by the machinery meant to protect it.

    Same shape as note_backoff and for the same reason: "deliberately not
    polling" and "wedged" look identical from outside, and the correct
    response is opposite. It is bounded rather than open-ended so that a hold
    nobody completes cannot silence the watcher for the rest of the
    fortnight — the one thing this project refuses is ambiguous silence.
    """
    state["hold_until"] = (utc_now() + timedelta(minutes=minutes)).isoformat()
    if event_slug is not None:
        state["hold_event_slug"] = event_slug
        state["hold_priority"] = int(priority)


def clear_hold(state: dict) -> None:
    state["hold_until"] = None
    state["hold_event_slug"] = None
    state["hold_priority"] = 0


def held_priority(state: dict) -> int:
    """How important the live hold is, or 0 if nothing is held.

    Reads 0 once the hold window has lapsed, so a stale marker cannot keep
    outranking a real find for the rest of the day.
    """
    if hold_remaining(state) <= 0:
        return 0
    return int(state.get("hold_priority") or 0)


def hold_remaining(state: dict) -> float:
    """Seconds left on a live hold, or 0.0 if none is running."""
    until = _parse(state.get("hold_until"))
    if until is None:
        return 0.0
    return max(0.0, (until - utc_now()).total_seconds())


# ── Day / night sessions ─────────────────────────────────────────────────────
#
# The watcher runs in two modes with different settings, and until now it
# crossed between them silently — a line in a log nobody reads. A change to
# how often it polls, and to how long it waits, is worth being told about at
# the moment it happens, together with what the finished session achieved.

#: Most listings kept per session. Enough to describe a busy day without
#: letting state.json grow without limit over a fortnight.
SESSION_LISTING_CAP = 20


def _session_defaults():
    return {
        "mode": None,             # "day" | "night"
        "started_at": None,       # ISO8601
        "checks": 0,              # page readings, not cycles
        "unhealthy": 0,
        "degraded": 0,
        "resale_blind": 0,
        "finds": 0,
        "blocks": 0,
        "listings": [],           # what was actually seen, with timestamps
    }


def session(state: dict) -> dict:
    return state.setdefault("session", _session_defaults())


def start_session(state: dict, mode: str) -> None:
    state["session"] = _session_defaults()
    state["session"]["mode"] = mode
    state["session"]["started_at"] = utc_now().isoformat()


def note_session_poll(state: dict, reading) -> None:
    """Fold one page reading into the running session totals."""
    s = session(state)
    s["checks"] = s.get("checks", 0) + 1
    if reading.failed or reading.degraded or reading.blocked:
        s["unhealthy"] = s.get("unhealthy", 0) + 1
    if reading.degraded:
        s["degraded"] = s.get("degraded", 0) + 1
    if reading.resale == UNKNOWN:
        s["resale_blind"] = s.get("resale_blind", 0) + 1
    if reading.blocked:
        s["blocks"] = s.get("blocks", 0) + 1


def note_session_find(state: dict, reading) -> None:
    """Record a find, and what it was.

    The listing text is kept because it is otherwise lost. A listing lives
    minutes; afterwards the only record of its section and price was the
    alert email. Keeping it here means the end-of-session summary can say
    what turned up and what it cost, which is how you learn what these
    actually go for.
    """
    s = session(state)
    s["finds"] = s.get("finds", 0) + 1
    seen = s.setdefault("listings", [])
    for listing in reading.listings:
        entry = f"{stamp()} — {reading.event_name}: {listing.describe()}"
        if entry not in seen:
            seen.append(entry)
    del seen[:-SESSION_LISTING_CAP]


def session_hours(state: dict) -> float:
    return _hours_since(session(state).get("started_at")) or 0.0


def note_next_poll(state: dict, seconds: float) -> None:
    """Record when the next poll is actually due.

    The watchdog used to judge staleness against a fixed 45 minutes, a number
    that only ever matched the daytime cadence. Overnight the interval is 30
    minutes jittered to 37.5, and the gap measured start-to-start includes the
    poll itself: a real 38-minute gap was observed on 2026-08-17, leaving
    seven minutes before a healthy watcher would have been restarted.

    Rather than keep a second copy of the cadence rules in a shell script and
    hope the two stay in step, the watcher simply says when it will be back.
    The watchdog then measures overdue-ness against that, which stays correct
    through night mode, a changed EP_POLL_SECONDS, or another watched page.
    """
    state["next_poll_due"] = (utc_now() + timedelta(seconds=seconds)).isoformat()


def note_profile_reset(state: dict) -> None:
    """Record that the browser identity was rebuilt just now."""
    state["profile_reset_at"] = utc_now().isoformat()


def backup_is_due(state: dict, every_hours: float = 24.0) -> bool:
    """Has it been long enough since the last snapshot? True on the first ever."""
    hours = _hours_since(state.get("last_backup_at"))
    return hours is None or hours >= every_hours


def note_backup(state: dict) -> None:
    """Record that a snapshot was taken just now."""
    state["last_backup_at"] = utc_now().isoformat()


def profile_age_minutes(state: dict) -> Optional[float]:
    """How old the current browser identity is, or None if never recorded."""
    hours = _hours_since(state.get("profile_reset_at"))
    return None if hours is None else hours * 60.0


def profile_is_stale(state: dict) -> bool:
    """Is the browser identity old enough to be refreshed before it is refused?

    Ticketmaster's bot-check cookies age out. Across 28 blocks in six days,
    every single one was cleared by a fresh profile on the first attempt, and
    the exponential backoff behind that reset was never once reached. So the
    wall is carried in the profile rather than in the IP, and waiting for it
    costs two resale-blind readings and a wasted cycle every time.

    Refreshing early is close to free — one cold page load during a sleep
    window — so the watcher steps around the wall instead of walking into it.
    The reactive reset stays as the backstop for the ones that beat the timer.
    """
    if not config.PROFILE_MAX_AGE_MINUTES:
        return False
    age = profile_age_minutes(state)
    # Never recorded: treat as fresh and start the clock rather than resetting
    # a profile that may be minutes old, which would throw away a good session
    # on every upgrade or restart.
    return age is not None and age >= config.PROFILE_MAX_AGE_MINUTES


def backoff_remaining(state: dict) -> float:
    """Seconds left of a deliberate backoff, or 0.0 if not resting."""
    until = _parse(state.get("backoff_until"))
    if until is None:
        return 0.0
    return max(0.0, (until - utc_now()).total_seconds())


def hours_since_check(state: dict):
    """How long since the watcher last did anything at all.

    The liveness signal. A hung process keeps its PID and satisfies launchd
    forever; this is what actually distinguishes working from wedged.
    """
    return _hours_since(state.get("last_check_at"))


# ── Connection health ────────────────────────────────────────────────────────
#
# The point of all this is one lesson learned the hard way on 2026-08-13: a
# watcher polling too fast got the *home* connection flagged, which blocked
# ordinary manual browsing. That is the worst possible outcome, because the
# home IP is the one needed to actually buy a ticket. So the watcher tracks
# how often it is being blocked and says so plainly, rather than leaving a
# run of quiet failures to be interpreted.

# ── Alternating networks ─────────────────────────────────────────────────────

def as_fingerprint(seen) -> dict:
    """Accept either a full fingerprint or a bare IP string.

    The IP-only form is what the older callers and the single-network tests
    pass, and what every state file written before connections were identified
    by their gateway contains. Treating the address as its own key keeps all
    of that working unchanged.
    """
    if isinstance(seen, dict):
        return seen
    return {"key": seen or "", "ip": seen or ""}


def note_network(state: dict, seen) -> str:
    """Record which connection this poll went out through.

    Returns "" if nothing changed, "readdressed" if the same connection was
    given a new public address, or "switched" if this is a different
    connection altogether.

    Those two used to be guessed at by comparing labels, which was wrong in
    both directions: a carrier re-addressing a tether looked like a switch,
    and a switch between two connections that happened to share a label looked
    like a re-address. Observed on 2026-08-18, when moving from the eir mobile
    hotspot onto a Sky line was announced as "new address, same connection".
    The gateway settles it — a different router is a different network.
    """
    fp = as_fingerprint(seen)
    key, ip = fp.get("key") or fp.get("ip") or "", fp.get("ip") or ""
    if not key:
        return ""

    networks = dict(state.get("networks", {}))
    was_key = state.get("current_net") or state.get("current_ip")
    was_ip = state.get("current_ip")

    # Upgrading a state file written when the public address WAS the identity.
    # The same address means the same connection, so adopt the gateway as its
    # key, carry its history across, and do not announce a switch that never
    # happened — an upgrade should be invisible from the inbox.
    if not state.get("current_net") and was_ip and was_ip == ip and key != was_ip:
        legacy = networks.pop(was_ip, None)
        if legacy is not None:
            networks[key] = legacy
        was_key = key

    entry = dict(networks.get(key) or {})
    if not entry:
        entry = {"first_seen": utc_now().isoformat(), "searches": 0, "blocks": 0}
    if not entry.get("label"):
        # Named once, on first sight, and then left alone — so a connection
        # keeps the name it was given even after the watcher has moved on and
        # come back. config.NETWORK_NAMES still overrides it at read time.
        #
        # Set here rather than only for brand-new entries, because an entry
        # carried across from address-keyed state has no label either, and
        # without one it fell through to the old rule — which knew of exactly
        # two connections and so called a Sky line "the hotspot".
        entry["label"] = _guess_label(state, fp)

    # Remember every address this connection has been given. It is the honest
    # answer to "is this the same network?" when a tether is re-addressed six
    # times in an afternoon.
    addresses = list(entry.get("addresses") or ([was_ip] if entry.get("searches") and was_ip == ip else []))
    if ip and ip not in addresses:
        addresses.append(ip)
    entry["addresses"] = addresses[-10:]
    for field in ("gateway", "gateway_mac", "port", "interface", "subnet"):
        if fp.get(field):
            entry[field] = fp[field]

    changed = ""
    if was_key and was_key != key:
        changed = "switched"
    elif ip and was_ip and was_ip != ip:
        changed = "readdressed"

    if was_key != key:
        state["current_net"] = key
        state["current_ip_since"] = utc_now().isoformat()
        state["searches_on_current_ip"] = 0
        state["rotation_asked_at"] = None
    state["current_net"] = key
    state["current_ip"] = ip or was_ip

    state["searches_on_current_ip"] = state.get("searches_on_current_ip", 0) + 1
    entry["searches"] = entry.get("searches", 0) + 1
    entry["last_seen"] = utc_now().isoformat()
    networks[key] = entry
    state["networks"] = networks
    prune_networks(state)
    return changed


def _guess_label(state: dict, fp: dict) -> str:
    """A name for a connection nobody has named, good enough to act on.

    Deliberately modest. It guesses at exactly two things — a phone hotspot,
    which announces itself by its gateway, and the first connection the
    watcher ever saw, which is home if you started it at home — and otherwise
    describes the network by its private range rather than inventing a name
    for it. "The 192.168.0.x network" means something to whoever set up that
    router; "unknown network 3" means nothing to anybody.
    """
    if fp.get("hotspot"):
        return config.HOTSPOT_LABEL
    if config.HOME_NETWORK_IP and fp.get("ip") == config.HOME_NETWORK_IP:
        return config.HOME_NETWORK_LABEL
    if not state.get("networks"):
        return config.HOME_NETWORK_LABEL
    subnet = fp.get("subnet")
    port = (fp.get("port") or "").strip()
    if subnet and port:
        return f"the {subnet} network via {port}"
    if subnet:
        return f"the {subnet} network"
    # Nothing but an address to go on — an old state file, or a machine where
    # the gateway cannot be read. Fall back to the rule that held when there
    # were only ever two connections: the first is home, the next is the
    # hotspot. Wrong for a third, but no worse than it ever was.
    return config.HOTSPOT_LABEL


def network_label(state: dict, key=None) -> str:
    """Human name for a connection, however many of them there turn out to be.

    Resolution order, most explicit first: a name David configured, the name
    learned when the connection was first seen, then a guess. `key` may be a
    connection key or a bare public IP, so callers holding either still work.
    """
    key = key or state.get("current_net") or state.get("current_ip")
    if not key:
        return "an unknown connection"

    entry = (state.get("networks") or {}).get(key) or {}

    # Configured names win, and may be keyed on whichever identifier David had
    # to hand when he wrote them down.
    for candidate in (key, entry.get("gateway_mac"), entry.get("gateway"), entry.get("ip")):
        if candidate and str(candidate).lower() in config.NETWORK_NAMES:
            return config.NETWORK_NAMES[str(candidate).lower()]
    for address in entry.get("addresses") or []:
        if str(address).lower() in config.NETWORK_NAMES:
            return config.NETWORK_NAMES[str(address).lower()]

    # Legacy: a state file written before connections had gateways is keyed on
    # the public IP, and EP_HOME_IP is still how that deployment names home.
    if config.HOME_NETWORK_IP and config.HOME_NETWORK_IP in (
        key, entry.get("ip"), *(entry.get("addresses") or [])
    ):
        return config.HOME_NETWORK_LABEL

    if entry.get("label"):
        return entry["label"]
    if not state.get("networks"):
        return "this connection"
    if config.HOME_NETWORK_IP:
        # Home is already decided above by EP_HOME_IP. Anything else with no
        # label is a connection met before the watcher started naming them,
        # and saying so is better than asserting it was the hotspot — with
        # more than two connections that assertion is simply a guess wearing
        # a confident face.
        return f"an earlier connection ({key})"
    # No EP_HOME_IP either: fall back to the rule that held when there were
    # only ever two, which is the best available from an address alone.
    first = min(state["networks"].items(), key=lambda kv: kv[1].get("first_seen", ""))[0]
    return config.HOME_NETWORK_LABEL if key == first else config.HOTSPOT_LABEL


def describe_network(state: dict, key=None) -> str:
    """The label plus enough detail to recognise it, for an email."""
    key = key or state.get("current_net") or state.get("current_ip")
    entry = (state.get("networks") or {}).get(key) or {}
    label = network_label(state, key)
    # Only add detail the label does not already carry. An auto-generated name
    # is built from exactly these parts, so appending them produced "the
    # 192.168.0.x network via Wi-Fi (192.168.0.x, via Wi-Fi)".
    bits = [b for b in (entry.get("subnet"),
                        f"via {entry['port']}" if entry.get("port") else "")
            if b and b not in label]
    return f"{label} ({', '.join(bits)})" if bits else label


#: How long a connection with nothing against it is remembered after it was
#: last used. Anything that ever drew a block is kept regardless — that is the
#: history worth having — but a tether that was re-addressed six times in an
#: afternoon leaves six entries that mean nothing a few days later, and a list
#: nobody can read is a list nobody reads.
NETWORK_FORGET_DAYS = 3.0


def prune_networks(state: dict) -> None:
    """Drop connections long unused that never caused any trouble."""
    current = state.get("current_net") or state.get("current_ip")
    kept = {}
    for key, entry in (state.get("networks") or {}).items():
        idle = _hours_since(entry.get("last_seen") or entry.get("first_seen"))
        if (key == current or entry.get("blocks")
                or idle is None or idle < NETWORK_FORGET_DAYS * 24):
            kept[key] = entry
    state["networks"] = kept


def naming_key(state: dict, key=None) -> str:
    """The identifier to put in EP_NETWORK_NAMES to name this connection.

    The gateway MAC is what the watcher actually keys on, so it is offered
    first; the gateway address is easier to recognise and works too. Printed
    in the email about joining a connection, because that is the moment David
    knows which network he is on and might want to name it — expecting him to
    go and find a router MAC later is expecting too much.
    """
    key = key or state.get("current_net") or state.get("current_ip")
    entry = (state.get("networks") or {}).get(key) or {}
    return entry.get("gateway_mac") or entry.get("gateway") or entry.get("ip") or key or ""


def is_named(state: dict, key=None) -> bool:
    """Has David named this connection, as opposed to the watcher guessing?

    Deliberately strict: only an explicit EP_NETWORK_NAMES entry or EP_HOME_IP
    counts. A guessed name is often right and always worth showing, but it is
    still a guess, and offering to replace it costs nothing while pretending
    it is settled could leave two connections sharing one name.
    """
    key = key or state.get("current_net") or state.get("current_ip")
    entry = (state.get("networks") or {}).get(key) or {}
    for candidate in (key, entry.get("gateway_mac"), entry.get("gateway"), entry.get("ip")):
        if candidate and str(candidate).lower() in config.NETWORK_NAMES:
            return True
    for address in entry.get("addresses") or []:
        if str(address).lower() in config.NETWORK_NAMES:
            return True
    return bool(config.HOME_NETWORK_IP) and config.HOME_NETWORK_IP in (
        key, entry.get("ip"), *(entry.get("addresses") or [])
    )


def known_networks(state: dict) -> list:
    """[(label, key, searches, blocks, is_current)] for everything ever seen.

    So an email can show the whole picture rather than only the connection in
    use — which is the question that actually matters when one of them is
    flagged and David has to choose a different one to buy on.
    """
    current = state.get("current_net") or state.get("current_ip")
    rows = []
    for key, entry in (state.get("networks") or {}).items():
        rows.append((
            network_label(state, key),
            key,
            entry.get("searches", 0),
            entry.get("blocks", 0),
            key == current,
        ))
    # Current first, then busiest — the order someone reads them in.
    return sorted(rows, key=lambda r: (not r[4], -r[2]))


#: What to say when there is no specific connection worth naming as the
#: destination. Deliberately vague, because vague and true beats precise and
#: useless.
ANY_OTHER_NETWORK = "a different network"


def other_network_label(state: dict) -> str:
    """What to suggest switching TO.

    Only ever names a connection David can actually act on: one he has named,
    or the home Wi-Fi and hotspot the watcher knows how to describe joining.

    An entry labelled by its address is a connection the watcher met once and
    cannot tell him how to rejoin. Sitting on a third network with a day-old
    tether in its history, this returned "an earlier connection
    (212.129.87.241)" — so the instruction would have read "move the MacBook to
    an earlier connection (212.129.87.241)", which is not an instruction at all.

    Ranked on blocks in the last 24 hours rather than the lifetime tally, which
    never decays. The question is which connection is safe to move to now, not
    which has the cleanest record since the watcher started.
    """
    current = state.get("current_net") or state.get("current_ip")
    here = network_label(state, current)

    candidates = []
    for label, key, searches, _lifetime, is_current in known_networks(state):
        if is_current or label == here:
            continue
        actionable = is_named(state, key) or label in (
            config.HOME_NETWORK_LABEL, config.HOTSPOT_LABEL
        )
        if actionable:
            candidates.append((recent_blocks(state, 24, ip=key), -searches, label))

    clean = [c for c in candidates if c[0] == 0]
    if clean:
        return min(clean)[2]

    # Everything it can name is either in use or currently in trouble. The
    # hotspot is the one connection that can always be produced on demand, so
    # suggest that — unless it is what he is already on.
    if here != config.HOTSPOT_LABEL:
        return config.HOTSPOT_LABEL
    return ANY_OTHER_NETWORK


def should_rotate_network(state: dict) -> tuple:
    """Is it time to switch the MacBook to the other connection?

    Returns (bool, reason). Triggers on either elapsed time or number of
    searches, whichever comes first — the search count is what actually
    correlates with getting rate-limited, but the clock catches the case
    where the cadence has been slowed right down.
    """
    if not (state.get("current_net") or state.get("current_ip")):
        return False, ""

    hours = _hours_since(state.get("current_ip_since"))
    searches = state.get("searches_on_current_ip", 0)

    reasons = []
    if hours is not None and hours >= config.NETWORK_ROTATE_HOURS:
        reasons.append(f"{hours:.1f}h on this connection")
    if searches >= config.NETWORK_ROTATE_SEARCHES:
        reasons.append(f"{searches} searches from it")
    if not reasons:
        return False, ""

    # Already asked recently and he hasn't switched — don't repeat it every
    # hour, that just trains him to ignore the section.
    asked = _hours_since(state.get("rotation_asked_at"))
    if asked is not None and asked < config.NETWORK_ROTATE_HOURS:
        return False, ""

    return True, " and ".join(reasons)


def mark_rotation_asked(state: dict) -> None:
    state["rotation_asked_at"] = utc_now().isoformat()


#: Shortest gap between two "same connection, new address" emails. A genuine
#: home-to-hotspot switch is never suppressed; this only guards against a
#: carrier re-addressing a tether repeatedly, which would otherwise fill the
#: inbox with mail about something David did not do — and an alert channel
#: that cries wolf is one he stops reading.
#:
#: Ten minutes turned out not to be a guard at all. Once the watcher moved onto
#: a mobile connection on 2026-08-18, the carrier re-addressed it at 10:35,
#: 10:48 and 11:26 — three emails in fifty minutes, none of them about
#: anything David had done or could act on, on top of the hourly report. An
#: hour is long enough that a churning tether is mentioned rather than
#: narrated, and it costs nothing that matters: the address itself is in every
#: hourly report, and a real switch between connections still emails instantly.
READDRESS_EMAIL_MIN_MINUTES = 60.0


def should_email_network(state: dict, readdressed: bool) -> bool:
    """Is this connection change worth an email right now?

    A real switch between connections always is: it changes where blocks
    land, and which connection is safe to buy on. A same-connection
    re-address is worth saying once, but not every few minutes.
    """
    if not readdressed:
        return True
    since = _hours_since(state.get("last_network_email_at"))
    return since is None or since * 60 >= READDRESS_EMAIL_MIN_MINUTES


def mark_network_emailed(state: dict) -> None:
    state["last_network_email_at"] = utc_now().isoformat()


def network_status(state: dict) -> tuple:
    """Return (should_switch, headline, instruction) for the email."""
    ip = state.get("current_net") or state.get("current_ip")
    if not ip:
        return False, "Could not determine which connection is in use.", ""

    label = network_label(state)
    hours = _hours_since(state.get("current_ip_since")) or 0.0
    searches = state.get("searches_on_current_ip", 0)
    # The public address, not the connection key — the key is a router MAC,
    # which identifies the network precisely and means nothing to a reader.
    address = state.get("current_ip") or "address unknown"
    headline = f"On {label} ({address}) — {searches} searches over {hours:.1f}h."

    switch, reason = should_rotate_network(state)
    if not switch:
        return False, headline, "No need to change anything."

    other = other_network_label(state)
    if other == config.HOTSPOT_LABEL:
        how = (
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  iPhone's Personal Hotspot. (On the phone: Settings > Personal Hotspot.)"
        )
    elif other == config.HOME_NETWORK_LABEL:
        how = (
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  home network again. You can turn the phone's hotspot back off."
        )
    elif other == ANY_OTHER_NETWORK:
        how = (
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  home Wi-Fi, or any other network you trust. The watcher recognises\n"
            "  whatever it lands on and starts its counters again."
        )
    else:
        # A connection David has named. The watcher cannot know how to join it,
        # so it names it and gets out of the way — and leaves the choice open,
        # because any connection it has not met is equally good.
        how = (
            f"  On the MacBook: click the Wi-Fi icon in the menu bar and pick\n"
            f"  {other}, your iPhone's Personal Hotspot, or any other network you\n"
            f"  trust. The watcher recognises whatever it lands on."
        )

    return (
        True,
        headline,
        f"TIME TO SWITCH NETWORKS — {reason}.\n\n"
        f"  Move the MacBook from {label} to your {other}.\n\n"
        f"{how}\n\n"
        "  Nothing else to do. The watcher notices the new connection by itself,\n"
        "  resets its counters, and will tell you when to switch back.\n"
        "  Splitting the load across both keeps either from being rate-limited,\n"
        "  and leaves you a working connection to buy on if one does get flagged.",
    )


def _block_entries(state: dict) -> list:
    """Block history as (timestamp, ip, net) triples, whatever shape it is on disk.

    Three generations of entry have to be readable at once. The oldest are
    bare ISO strings with no connection attached; then came {"at", "ip"}; now
    {"at", "ip", "net"}, keyed on the connection rather than the address it
    happened to hold at the time. An entry with no connection is read as
    belonging to none, and counted against whichever one is current — the
    conservative reading, since over-warning about the connection in use is
    much cheaper than staying quiet about it.
    """
    out = []
    for entry in state.get("block_history", []):
        if isinstance(entry, dict):
            out.append((entry.get("at"), entry.get("ip"), entry.get("net")))
        else:
            out.append((entry, None, None))
    return out


def record_block(state: dict, when=None) -> None:
    """Note an HTTP 403 against the connection it happened on, and prune.

    `when` overrides the timestamp and exists for the tests, which need to
    record several distinct episodes without waiting two minutes between each.

    The timestamp alone is not enough. Blocks follow the connection, not the
    clock: after switching from a flagged home Wi-Fi to a clean hotspot, a
    time-only history keeps reporting the block for another 24 hours — and it
    reports it against the new connection, which is the opposite of the truth
    and directly contradicts the advice to switch.
    """
    history = _block_entries(state)

    # One wall, one entry. handle() runs per watched page, so a single 403
    # used to be written twice — and connection_health() reads those counts
    # against thresholds set when one page was watched, so a lone episode
    # already graded "watch" and a third page would have graded it "blocked",
    # whose advice is to stop the watcher. Count episodes, not readings.
    now = when or utc_now()
    if history:
        last = _parse(history[-1][0])
        if last is not None and (now - last).total_seconds() < BLOCK_EPISODE_SECONDS:
            return

    key = state.get("current_net") or state.get("current_ip")
    history.append((now.isoformat(), state.get("current_ip"), key))
    cutoff = utc_now() - timedelta(days=7)
    state["block_history"] = [
        {"at": ts, "ip": ip, "net": net}
        for ts, ip, net in history if (_parse(ts) or utc_now()) >= cutoff
    ]

    # Also kept as a running per-connection tally, which survives the 7-day
    # prune and answers "which of them is burnt" at a glance.
    if key:
        networks = dict(state.get("networks", {}))
        entry = networks.get(key) or {"first_seen": utc_now().isoformat(),
                                      "searches": 0, "blocks": 0}
        entry["blocks"] = entry.get("blocks", 0) + 1
        networks[key] = entry
        state["networks"] = networks


#: How close together two 403s have to be to count as the same wall. A cycle
#: polls every page in a few seconds, so anything inside two minutes is one
#: episode seen more than once.
BLOCK_EPISODE_SECONDS = 120.0

#: Sentinel for recent_blocks(ip=...): count every connection.
ANY_IP = object()


def recent_blocks(state: dict, hours: float = 24.0, ip=ANY_IP) -> int:
    """Blocks in the last `hours`. Pass `ip` to count only that connection.

    An unattributed entry (from before blocks carried an IP) counts for any
    connection asked about, so old history never silently disappears.
    """
    if ip is ANY_IP:
        cutoff = utc_now() - timedelta(hours=hours)
        return sum(1 for ts, _, _ in _block_entries(state)
                   if (_parse(ts) or cutoff) >= cutoff)
    mine, unknown = _block_counts(state, hours, ip)
    return mine + unknown


def first_seen(state: dict, ip) -> Optional[datetime]:
    """When this connection was first used, if we have ever seen it."""
    entry = (state.get("networks") or {}).get(ip) or {}
    return _parse(entry.get("first_seen"))


def _block_counts(state: dict, hours: float, ip) -> tuple:
    """(attributed_to_ip, unattributed) blocks inside the window.

    Kept apart so the wording can stay honest. An entry written before blocks
    carried an IP still counts — going quiet about a real block is the
    expensive mistake — but it cannot support a sentence naming the
    connection it happened on, because nothing recorded which one that was.

    An unattributed block from before this connection was ever seen is a
    different matter: it definitely did not happen here. Counting it anyway
    told a hotspot first used at 11:47 that it had four blocks from the
    previous afternoon, which is both impossible and precisely the
    misattribution this whole per-connection accounting exists to stop.
    """
    cutoff = utc_now() - timedelta(hours=hours)
    since = first_seen(state, ip)
    # Addresses this connection has held. A block recorded before connections
    # were identified names only an address, and that address may well belong
    # to the connection being asked about — dropping it would quietly lose a
    # week of history at the moment of the upgrade.
    held = set((state.get("networks") or {}).get(ip, {}).get("addresses") or [])
    mine = unknown = 0
    for ts, at_ip, at_net in _block_entries(state):
        when = _parse(ts)
        if when is None or when < cutoff:
            continue
        # The connection is the truth where it was recorded; the address is
        # the fallback for entries written before connections were identified.
        attributed = at_net or at_ip
        if attributed is None:
            if since is not None and when < since:
                continue
            unknown += 1
        elif attributed == ip or at_ip == ip or (at_ip and at_ip in held):
            mine += 1
    return mine, unknown


def blocks_elsewhere(state: dict, hours: float = 24.0) -> list:
    """[(label, ip, count)] for connections other than the current one.

    So an email can say "your home Wi-Fi is the one in trouble, you are now on
    the hotspot and it is clean" instead of leaving David to work out which of
    the two a block belonged to.
    """
    current = state.get("current_net") or state.get("current_ip")
    cutoff = utc_now() - timedelta(hours=hours)
    counts = {}
    for ts, at_ip, at_net in _block_entries(state):
        attributed = at_net or at_ip
        if attributed is None or attributed == current or at_ip == current:
            continue
        if (_parse(ts) or cutoff) < cutoff:
            continue
        counts[attributed] = counts.get(attributed, 0) + 1
    return [(network_label(state, key), key, n) for key, n in sorted(counts.items())]


def connection_health(state: dict) -> tuple:
    """Return (severity, headline, what-to-do) for the CURRENT connection.

    Severity is one of "ok", "watch", "blocked" — used to decide whether an
    email needs to shout.

    Counted per connection, not per hour of wall clock. Counting every block
    regardless of where it happened meant that switching networks — the exact
    thing the watcher had just asked for — produced an email telling him the
    fresh connection was rate-limited, on the strength of blocks the old one
    had collected. Advice that punishes the reader for following it is worse
    than none.
    """
    ip = state.get("current_net") or state.get("current_ip")
    day_mine, day_unknown = _block_counts(state, 24, ip)
    hour_mine, hour_unknown = _block_counts(state, 1, ip)
    day, hour = day_mine + day_unknown, hour_mine + hour_unknown
    here = network_label(state) if ip else "this connection"

    # Name the connection only when the history actually says so. Asserting
    # "4 blocks on your phone hotspot" about entries that recorded no
    # connection at all is a confident claim built on nothing, and it would
    # point him away from the connection that is really burnt.
    on_here = f" on {here}" if day and not day_unknown else ""

    # Whatever the current connection's verdict, name any other connection
    # that is in trouble. That is the one he must not go and buy on.
    elsewhere = blocks_elsewhere(state, 24)
    others = ""
    if elsewhere:
        others = "\n\n  " + "\n  ".join(
            f"Note: {label} ({other_ip}) took {n} block(s) in the last 24h — "
            f"that is the connection in trouble, not this one."
            for label, other_ip, n in elsewhere
        )

    if day == 0:
        return (
            "ok",
            f"No blocks on {here} in the last 24 hours — it looks healthy." + others,
            "Nothing to do.",
        )

    # No blocks in the last hour means recovered, however many there were
    # earlier in the day. Gating this on a low 24h count meant that after a
    # busy day the next branch fired instead and reported "0 blocks in the
    # last hour ... being rate-limited" — a sentence that contradicts its own
    # first clause, and the sort of thing that teaches you to skim past the
    # health line entirely.
    if hour == 0:
        return (
            "watch",
            f"{day} block(s){on_here} in the last 24h, none in the last hour "
            f"— recovered." + others,
            "Nothing to do. The watcher backs off and resets its browser profile "
            "on its own when this happens.",
        )

    if hour <= 2:
        return (
            "watch",
            f"{hour} block(s){on_here} in the last hour ({day} in 24h) "
            f"— being rate-limited." + others,
            "No action needed yet: the watcher is already backing off and will "
            "resume by itself. If you need to browse Ticketmaster right now, use "
            "mobile data rather than this connection.",
        )

    return (
        "blocked",
        f"{hour} blocks{on_here} in the last hour ({day} in 24h) "
        f"— this connection is blocked." + others,
        "Act on this one:\n"
        "  1. Stop the watcher. Repeated attempts extend the block.\n"
        "     macOS:  launchctl unload ~/Library/LaunchAgents/com.davidcoyne.ep2026watcher.plist\n"
        "  2. To browse or buy right now, switch to mobile data — a phone with\n"
        "     Wi-Fi off, or tethered. That is a different IP and works immediately.\n"
        "  3. Sign in to your Ticketmaster account. An authenticated session gets\n"
        "     considerably more latitude than anonymous browsing.\n"
        "  4. Leave it a few hours. These blocks decay on their own.\n"
        "  5. Before restarting, raise EP_POLL_SECONDS. Getting blocked on day two\n"
        "     catches nothing on day nine.",
    )
