"""Run-to-run memory: what we last saw, and how the watcher itself is doing.

Kept as plain JSON with defaults filled in for any missing key, so adding a
field here never needs a migration and deleting state.json is always safe.
"""

import json
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
        # When the browser profile was last thrown away and rebuilt. The
        # bot-check cookies age out, so this drives a pre-emptive refresh —
        # see config.PROFILE_MAX_AGE_MINUTES.
        "profile_reset_at": None,         # ISO8601
        # When the next poll is due. Lets the watchdog judge lateness against
        # the cadence actually in force — which changes overnight — instead
        # of a fixed threshold that only ever matched the daytime one.
        "next_poll_due": None,            # ISO8601
    }


def load() -> dict:
    state = _defaults()
    try:
        with open(config.STATE_FILE, "r") as f:
            state.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return state


def save(state: dict) -> None:
    try:
        config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(config.STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        print(f"[{stamp()}] WARNING: could not save state: {exc}")


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
        # Failures are counted per event too, and that is not a detail.
        # A single shared counter meant a healthy page reset the count a
        # broken one had just incremented — so one page could fail on every
        # cycle for a fortnight while the watchdog never fired and the hourly
        # email reported everything fine. Reproduced before it was fixed.
        "consecutive_failures": 0,
    })


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

def note_network(state: dict, ip) -> bool:
    """Record which connection this poll went out through.

    Returns True if the connection changed since last time — which is how the
    watcher notices David has switched networks without him having to tell it.
    A switch resets the counters and the nag, so the advice in the next email
    reflects the new connection rather than the old one.
    """
    if not ip:
        return False

    networks = dict(state.get("networks", {}))
    entry = networks.get(ip) or {"first_seen": utc_now().isoformat(), "searches": 0, "blocks": 0}

    changed = state.get("current_ip") not in (None, ip)
    if state.get("current_ip") != ip:
        state["current_ip"] = ip
        state["current_ip_since"] = utc_now().isoformat()
        state["searches_on_current_ip"] = 0
        state["rotation_asked_at"] = None

    state["searches_on_current_ip"] = state.get("searches_on_current_ip", 0) + 1
    entry["searches"] = entry.get("searches", 0) + 1
    networks[ip] = entry
    state["networks"] = networks
    return changed


def network_label(state: dict, ip=None) -> str:
    """Human name for a connection: "home Wi-Fi" or "phone hotspot".

    There is no reliable way to tell a tethered phone from a home router by IP
    alone, so this uses the simple rule that fits how it is actually used: the
    first connection the watcher ever saw is home (set EP_HOME_IP to override),
    and anything else is the hotspot.
    """
    ip = ip or state.get("current_ip")
    if not ip:
        return "an unknown connection"
    if config.HOME_NETWORK_IP:
        return "home Wi-Fi" if ip == config.HOME_NETWORK_IP else "phone hotspot"

    networks = state.get("networks", {})
    if not networks:
        return "this connection"
    first = min(networks.items(), key=lambda kv: kv[1].get("first_seen", ""))[0]
    return "home Wi-Fi" if ip == first else "phone hotspot"


def other_network_label(state: dict) -> str:
    return "phone hotspot" if network_label(state) == "home Wi-Fi" else "home Wi-Fi"


def should_rotate_network(state: dict) -> tuple:
    """Is it time to switch the MacBook to the other connection?

    Returns (bool, reason). Triggers on either elapsed time or number of
    searches, whichever comes first — the search count is what actually
    correlates with getting rate-limited, but the clock catches the case
    where the cadence has been slowed right down.
    """
    if not state.get("current_ip"):
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
    ip = state.get("current_ip")
    if not ip:
        return False, "Could not determine which connection is in use.", ""

    label = network_label(state)
    hours = _hours_since(state.get("current_ip_since")) or 0.0
    searches = state.get("searches_on_current_ip", 0)
    headline = f"On {label} ({ip}) — {searches} searches over {hours:.1f}h."

    switch, reason = should_rotate_network(state)
    if not switch:
        return False, headline, "No need to change anything."

    other = other_network_label(state)
    if other == "phone hotspot":
        how = (
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  iPhone's Personal Hotspot. (On the phone: Settings > Personal Hotspot.)"
        )
    else:
        how = (
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  home network again. You can turn the phone's hotspot back off."
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
    """Block history as (timestamp, ip) pairs, whatever shape it is on disk.

    Entries used to be bare ISO strings with no connection attached. Those are
    read as belonging to no particular connection, and counted against
    whichever one is current — the conservative reading, since over-warning
    about the connection in use is much cheaper than staying quiet about it.
    """
    out = []
    for entry in state.get("block_history", []):
        if isinstance(entry, dict):
            out.append((entry.get("at"), entry.get("ip")))
        else:
            out.append((entry, None))
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

    history.append((now.isoformat(), state.get("current_ip")))
    cutoff = utc_now() - timedelta(days=7)
    state["block_history"] = [
        {"at": ts, "ip": ip} for ts, ip in history if (_parse(ts) or utc_now()) >= cutoff
    ]

    # Also kept as a running per-connection tally, which survives the 7-day
    # prune and answers "which of the two is burnt" at a glance.
    ip = state.get("current_ip")
    if ip:
        networks = dict(state.get("networks", {}))
        entry = networks.get(ip) or {"first_seen": utc_now().isoformat(), "searches": 0, "blocks": 0}
        entry["blocks"] = entry.get("blocks", 0) + 1
        networks[ip] = entry
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
        return sum(1 for ts, _ in _block_entries(state)
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
    mine = unknown = 0
    for ts, at_ip in _block_entries(state):
        when = _parse(ts)
        if when is None or when < cutoff:
            continue
        if at_ip is None:
            if since is not None and when < since:
                continue
            unknown += 1
        elif at_ip == ip:
            mine += 1
    return mine, unknown


def blocks_elsewhere(state: dict, hours: float = 24.0) -> list:
    """[(label, ip, count)] for connections other than the current one.

    So an email can say "your home Wi-Fi is the one in trouble, you are now on
    the hotspot and it is clean" instead of leaving David to work out which of
    the two a block belonged to.
    """
    current = state.get("current_ip")
    cutoff = utc_now() - timedelta(hours=hours)
    counts = {}
    for ts, at_ip in _block_entries(state):
        if at_ip is None or at_ip == current:
            continue
        if (_parse(ts) or cutoff) < cutoff:
            continue
        counts[at_ip] = counts.get(at_ip, 0) + 1
    return [(network_label(state, ip), ip, n) for ip, n in sorted(counts.items())]


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
    ip = state.get("current_ip")
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
