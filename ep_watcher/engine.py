"""Orchestration: run the sources, merge their answers, decide who to wake up."""

from typing import List, Optional

from . import config, notify, state as state_mod
from .model import Reading, better_status
from .sources import browser, discovery, inventory_api
from .state import stamp


def merge(readings: List[Reading]) -> Reading:
    real = [r for r in readings if not r.failed]
    merged = Reading(source=" + ".join(r.source for r in readings) or "none")

    merged.blocked = any(r.blocked for r in readings)

    if not real:
        merged.failed = True
        merged.needs_login = any(r.needs_login for r in readings)
        for r in readings:
            merged.notes.extend(f"[{r.source}] {n}" for n in r.notes)
        return merged

    for r in real:
        merged.primary = better_status(merged.primary, r.primary)
        merged.resale = better_status(merged.resale, r.resale)
        merged.listings.extend(r.listings)
    for r in readings:
        merged.notes.extend(f"[{r.source}] {n}" for n in r.notes)
    merged.needs_login = any(r.needs_login for r in real)
    return merged


def poll(session: Optional[browser.BrowserSession] = None) -> Reading:
    """Ask every configured source once and merge the answers.

    The API sources run first and cost one HTTP call each; the browser is the
    expensive one. With EP_USE_BROWSER=0 the browser is skipped entirely,
    which is what lets the watcher run somewhere that has no Chrome — at the
    cost of everything only the browser can see.
    """
    readings = []

    if discovery.configured():
        readings.append(discovery.check())
    if inventory_api.configured():
        readings.append(inventory_api.check())

    if config.USE_BROWSER:
        readings.append(session.check() if session else browser.check())
    elif not readings:
        stub = Reading(source="none")
        stub.failed = True
        stub.note("browser disabled and no API key set — nothing can answer")
        readings.append(stub)

    return merge(readings)


def handle(reading: Reading, st: dict) -> None:
    """Fold a reading into state and fire whatever alerts it earns."""
    print(f"[{stamp()}] {reading.summary()}")
    for note in reading.notes:
        print(f"    {note}")

    state_mod.start_heartbeat_clock(st)
    state_mod.note_check(st, reading.failed)

    if reading.failed:
        failures = state_mod.record_failure(st)
        print(f"[{stamp()}] check failed ({failures} in a row)")
        if state_mod.should_alert_watchdog(st):
            if reading.blocked:
                reason = (
                    "Ticketmaster is rate-limiting this machine (HTTP 403). The watcher "
                    "is backing off automatically and will resume on its own — this "
                    "usually clears within a few hours. If it persists for a day, lower "
                    "the polling rate with EP_POLL_SECONDS."
                )
            elif reading.needs_login:
                reason = "The Ticketmaster session needs a human — it is logged out or challenged."
            else:
                reason = "Could not get a usable reading from Ticketmaster."
            notify.watchdog(reason, failures)
            st["last_watchdog_alert"] = state_mod.utc_now().isoformat()
        # A run of failures must not suppress the hourly report — a silent
        # watcher and a broken one look identical from the inbox, which is
        # precisely how the last one hid for 44 days.
        _maybe_heartbeat(reading, st)
        return

    was_broken = st["consecutive_failures"]
    new_listings = state_mod.record_success(st, reading)
    if was_broken >= config.WATCHDOG_FAILURE_THRESHOLD:
        notify.recovered(was_broken)
        st["last_watchdog_alert"] = None

    should, reason = state_mod.should_alert_availability(st, reading)
    if not should:
        print(f"[{stamp()}] nothing to report")
        _maybe_heartbeat(reading, st)
        return

    # A reserve that actually succeeded is a different, much more urgent email
    # than "a listing appeared" — there is a live hold with a countdown on it.
    if any("RESERVE ACCEPTED" in n for n in reading.notes):
        notify.reserved_in_browser(reading)
    else:
        notify.available(reading, reason, new_listings)
    st["last_availability_alert"] = state_mod.utc_now().isoformat()

    # A ticket turned up and David has been told properly. Restart the hourly
    # clock rather than following the good news with "no success this hour".
    state_mod.reset_heartbeat(st)


def _maybe_heartbeat(reading: Reading, st: dict) -> None:
    if not state_mod.should_send_heartbeat(st):
        return
    hours = state_mod.hours_since_heartbeat(st) or config.HEARTBEAT_HOURS
    checks = st["checks_since_heartbeat"]
    failures = st["failures_since_heartbeat"]
    print(f"[{stamp()}] hourly report: {checks} checks, {failures} failed")
    notify.heartbeat(checks, failures, hours, reading)
    state_mod.reset_heartbeat(st)


def run_once(session: Optional[browser.BrowserSession] = None) -> Reading:
    st = state_mod.load()
    try:
        reading = poll(session)
        handle(reading, st)
        return reading
    finally:
        state_mod.save(st)
