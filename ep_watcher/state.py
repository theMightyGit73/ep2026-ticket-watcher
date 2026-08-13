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

def should_alert_availability(state: dict, reading) -> tuple:
    """Decide whether this reading deserves an email, and why.

    Edge-triggered on each market independently, so resale appearing still
    alerts even on a run where primary was already available (and vice versa).
    Then re-nags on a slow clock while the good state persists — one missed
    push notification shouldn't cost the ticket, but neither should a stuck
    'available' spam the inbox every minute for a day.
    """
    reasons = []
    if reading.primary in GOOD_STATUSES and state["last_primary"] not in GOOD_STATUSES:
        reasons.append(f"primary stock went {state['last_primary']} → {reading.primary}")
    if reading.resale in GOOD_STATUSES and state["last_resale"] not in GOOD_STATUSES:
        reasons.append(f"resale went {state['last_resale']} → {reading.resale}")

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


def record_success(state: dict, reading) -> list:
    """Fold a good reading into state. Returns newly-seen listing descriptions."""
    state["consecutive_failures"] = 0
    state["last_success"] = utc_now().isoformat()

    current = [l.describe() for l in reading.listings]
    previous = set(state.get("known_listings", []))
    new = [c for c in current if c not in previous]

    state["known_listings"] = current
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
