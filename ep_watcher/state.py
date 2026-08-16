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
        "last_primary": UNKNOWN,
        "last_resale": UNKNOWN,
        "last_availability_alert": None,  # ISO8601
        "known_listings": [],
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
        "stop_notified": False,           # the final "watcher stopped" email
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
        since = _hours_since(ev["last_availability_alert"])
        if since is None or since >= config.AVAILABILITY_RENAG_HOURS:
            return True, "still available — periodic reminder"

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


def record_block(state: dict) -> None:
    """Note an HTTP 403 and prune history older than a week."""
    history = list(state.get("block_history", []))
    history.append(utc_now().isoformat())
    cutoff = utc_now() - timedelta(days=7)
    state["block_history"] = [
        ts for ts in history if (_parse(ts) or utc_now()) >= cutoff
    ]

    # Attribute the block to the connection it happened on, so the emails can
    # say "your home Wi-Fi is the one in trouble" rather than leaving it
    # ambiguous which of the two got flagged.
    ip = state.get("current_ip")
    if ip:
        networks = dict(state.get("networks", {}))
        entry = networks.get(ip) or {"first_seen": utc_now().isoformat(), "searches": 0, "blocks": 0}
        entry["blocks"] = entry.get("blocks", 0) + 1
        networks[ip] = entry
        state["networks"] = networks


def recent_blocks(state: dict, hours: float = 24.0) -> int:
    cutoff = utc_now() - timedelta(hours=hours)
    return sum(
        1 for ts in state.get("block_history", []) if (_parse(ts) or cutoff) >= cutoff
    )


def connection_health(state: dict) -> tuple:
    """Return (severity, headline, what-to-do) for the current connection.

    Severity is one of "ok", "watch", "blocked" — used to decide whether an
    email needs to shout.
    """
    day = recent_blocks(state, 24)
    hour = recent_blocks(state, 1)

    if day == 0:
        return (
            "ok",
            "No blocks in the last 24 hours — this connection looks healthy.",
            "Nothing to do.",
        )

    if hour == 0 and day <= 3:
        return (
            "watch",
            f"{day} block(s) in the last 24h, none in the last hour — recovered.",
            "Nothing to do. The watcher backs off on its own when this happens.",
        )

    if hour <= 2:
        return (
            "watch",
            f"{hour} block(s) in the last hour ({day} in 24h) — being rate-limited.",
            "No action needed yet: the watcher is already backing off and will "
            "resume by itself. If you need to browse Ticketmaster right now, use "
            "mobile data rather than this connection.",
        )

    return (
        "blocked",
        f"{hour} blocks in the last hour ({day} in 24h) — this connection is blocked.",
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
