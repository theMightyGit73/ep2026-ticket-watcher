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
        # Hourly "still nothing" report.
        "last_heartbeat": None,           # ISO8601
        "checks_since_heartbeat": 0,
        "failures_since_heartbeat": 0,
        # Connection health. Every HTTP 403 is recorded with a timestamp so
        # the emails can say whether this connection is in trouble, rather
        # than leaving David to infer it from a run of quiet failures.
        "block_history": [],              # ISO8601 timestamps, pruned to 7 days
        "checks_total": 0,
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
    reasons = []
    if reading.primary in GOOD_STATUSES and state["last_primary"] not in GOOD_STATUSES:
        reasons.append(f"primary stock went {state['last_primary']} → {reading.primary}")
    if reading.resale in GOOD_STATUSES and state["last_resale"] not in GOOD_STATUSES:
        reasons.append(f"resale went {state['last_resale']} → {reading.resale}")
    if new_listings:
        reasons.append(f"new listing(s): {', '.join(new_listings)}")

    if reasons:
        return True, "; ".join(reasons)

    if reading.any_good:
        since = _hours_since(state["last_availability_alert"])
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
    previous = set(state.get("known_listings", []))
    return [l.describe() for l in reading.listings if l.describe() not in previous]


def record_success(state: dict, reading) -> list:
    """Fold a good reading into state. Returns newly-seen listing descriptions.

    Call this AFTER should_alert_availability() — it overwrites the fields
    that decision compares against.
    """
    new = pending_listings(state, reading)
    state["consecutive_failures"] = 0
    state["last_success"] = utc_now().isoformat()
    state["known_listings"] = [l.describe() for l in reading.listings]
    state["last_primary"] = reading.primary
    state["last_resale"] = reading.resale
    return new


def record_failure(state: dict) -> int:
    state["consecutive_failures"] += 1
    return state["consecutive_failures"]


# ── Hourly heartbeat ─────────────────────────────────────────────────────────

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


def note_check(state: dict, failed: bool) -> None:
    state["checks_since_heartbeat"] += 1
    if failed:
        state["failures_since_heartbeat"] += 1


def reset_heartbeat(state: dict) -> None:
    state["last_heartbeat"] = utc_now().isoformat()
    state["checks_since_heartbeat"] = 0
    state["failures_since_heartbeat"] = 0


def hours_since_heartbeat(state: dict):
    return _hours_since(state["last_heartbeat"])


# ── Connection health ────────────────────────────────────────────────────────
#
# The point of all this is one lesson learned the hard way on 2026-08-13: a
# watcher polling too fast got the *home* connection flagged, which blocked
# ordinary manual browsing. That is the worst possible outcome, because the
# home IP is the one needed to actually buy a ticket. So the watcher tracks
# how often it is being blocked and says so plainly, rather than leaving a
# run of quiet failures to be interpreted.

def record_block(state: dict) -> None:
    """Note an HTTP 403 and prune history older than a week."""
    history = list(state.get("block_history", []))
    history.append(utc_now().isoformat())
    cutoff = utc_now() - timedelta(days=7)
    state["block_history"] = [
        ts for ts in history if (_parse(ts) or utc_now()) >= cutoff
    ]


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
