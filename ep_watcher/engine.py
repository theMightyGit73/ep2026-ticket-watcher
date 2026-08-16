"""Orchestration: run the sources, merge their answers, decide who to wake up."""

from typing import List, Optional

from . import config, liveness, network, notify, state as state_mod
from .model import Reading, better_status
from .sources import discovery, inventory_api
from .state import stamp

# `browser` is imported lazily, inside poll(), because it pulls in Playwright.
# In API-only mode (EP_USE_BROWSER=0) Playwright is not installed at all, and
# an import at module scope would kill the process before any source ran.


def merge(readings: List[Reading]) -> Reading:
    real = [r for r in readings if not r.failed]
    merged = Reading(source=" + ".join(r.source for r in readings) or "none")

    merged.blocked = any(r.blocked for r in readings)
    # Which sources came back empty-handed, kept even when others answered.
    # Without this the merged reading cannot tell "everything is fine" from
    # "the browser has been blocked for an hour and the free API is covering
    # for it", and those are very different amounts of cover.
    merged.failed_sources = [r.source for r in readings if r.failed]
    merged.answering_sources = [r.source for r in real]

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


def poll(session=None, event=None) -> Reading:
    """Ask every configured source about one event and merge the answers.

    The API sources run first and cost one HTTP call each; the browser is the
    expensive one. With EP_USE_BROWSER=0 the browser is skipped entirely,
    which is what lets the watcher run somewhere that has no Chrome — at the
    cost of everything only the browser can see.
    """
    event = event or config.EVENTS[0]
    readings = []

    if discovery.configured():
        readings.append(discovery.check(event))
    if inventory_api.configured():
        readings.append(inventory_api.check())

    if config.USE_BROWSER:
        from .sources import browser  # lazy: see the note at the top of the file

        readings.append(session.check(event) if session else browser.check(event))
    elif not readings:
        stub = Reading(source="none")
        stub.failed = True
        stub.note("browser disabled and no API key set — nothing can answer")
        readings.append(stub)

    merged = merge(readings)
    # merge() builds a fresh Reading, so the event identity has to be put back
    # or every alert would be unable to say which page it is about.
    merged.event_slug = event.slug
    merged.event_name = event.name
    merged.event_url = event.url
    return merged


def poll_all(session=None) -> list:
    """One reading per watched event.

    Every event is searched on every cycle rather than round-robin, so a
    listing on the quieter page is never up to a full cycle stale. The cost is
    paid in the interval instead — see config.poll_interval().
    """
    return [poll(session, event) for event in config.EVENTS]


def handle(reading: Reading, st: dict) -> None:
    """Fold a reading into state and fire whatever alerts it earns."""
    print(f"[{stamp()}] {reading.summary()}")
    for note in reading.notes:
        print(f"    {note}")

    state_mod.start_heartbeat_clock(st)
    # A partial reading counts as unhealthy, not as a clean poll. See
    # Reading.degraded — treating it as clean is what let a blocked browser
    # report "0 failed" for hours.
    state_mod.note_check(st, unhealthy=reading.failed or reading.degraded)
    if reading.degraded:
        state_mod.note_degraded(st, reading.failed_sources)
    state_mod.note_resale_visibility(st, reading)
    st["checks_total"] = st.get("checks_total", 0) + 1
    st["last_check_at"] = state_mod.utc_now().isoformat()

    # Which connection did this go out through? Detected rather than declared,
    # so switching the MacBook's network is all David has to do — the watcher
    # notices by itself and resets its counters.
    if config.USE_BROWSER:
        switched = state_mod.note_network(st, network.public_ip())
        if switched:
            print(f"[{stamp()}] network changed — now on {state_mod.network_label(st)}")

    if reading.blocked:
        state_mod.record_block(st)

    # Tell the outside world the Mac is still alive. Every local safeguard
    # assumes the laptop is on; this is the only signal that survives it
    # being shut, flat, or off the network.
    if config.USE_BROWSER:
        liveness.publish(f"poll {st.get('checks_total', 0)} at {stamp()}")

    if reading.failed:
        failures = state_mod.record_failure(st, reading)
        print(f"[{stamp()}] check failed ({failures} in a row)")
        _maybe_watchdog(reading, st, failures)
        # A run of failures must not suppress the hourly report — a silent
        # watcher and a broken one look identical from the inbox, which is
        # precisely how the last one hid for 44 days.
        _maybe_heartbeat(reading, st)
        return

    was_broken = st["consecutive_failures"]

    # Order matters and is load-bearing. record_success() overwrites
    # last_primary/last_resale/known_listings, which are exactly the fields
    # the alerting decision compares this reading against — so the decision
    # has to be made first, or every comparison is reading-vs-itself and the
    # edge detection quietly does nothing.
    new_listings = state_mod.pending_listings(st, reading)
    should, reason = state_mod.should_alert_availability(st, reading, new_listings)
    state_mod.record_success(st, reading, healthy=not reading.degraded)

    # A partial reading is still a reading: whatever did answer is recorded
    # and alerted on above, exactly as before. What changes is that it no
    # longer clears the failure counter, so a browser that stays blocked now
    # escalates instead of hiding behind the API that is covering for it.
    if reading.degraded:
        failures = st["consecutive_failures"]
        print(
            f"[{stamp()}] PARTIAL reading — {', '.join(reading.failed_sources)} "
            f"failed ({failures} in a row); still answered by "
            f"{', '.join(reading.answering_sources) or 'nothing'}"
        )
        _maybe_watchdog(reading, st, failures)
    elif was_broken >= config.WATCHDOG_FAILURE_THRESHOLD:
        notify.recovered(was_broken)
        st["last_watchdog_alert"] = None

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
    # Per event, or alerting on one page would start the re-nag clock for the
    # other and swallow its next find.
    state_mod.event_state(st, reading.event_slug)["last_availability_alert"] = (
        state_mod.utc_now().isoformat()
    )

    # A ticket turned up and David has been told properly. Restart the hourly
    # clock rather than following the good news with "no success this hour".
    state_mod.reset_heartbeat(st)


def watchdog_reason(reading: Reading) -> str:
    """Plain English for why the watcher is unhappy, in priority order.

    The degraded case is worded hardest on purpose. A total failure announces
    itself — the emails stop carrying readings. A partial one does the
    opposite: the hourly report keeps arriving, full of confident-looking
    UNAVAILABLE lines from an API that cannot see resale, while the only
    source that can has been walled for hours. That reads as good news.
    """
    if reading.blocked:
        cause = (
            "Ticketmaster is rate-limiting this machine (HTTP 403). The watcher "
            "is backing off automatically and will resume on its own — this "
            "usually clears within a few hours. If it persists for a day, lower "
            "the polling rate with EP_POLL_SECONDS."
        )
    elif reading.needs_login:
        cause = "The Ticketmaster session needs a human — it is logged out or challenged."
    elif reading.failed:
        cause = "Could not get a usable reading from Ticketmaster."
    else:
        cause = ""

    if not reading.degraded:
        return cause

    # Both facts, always. The cause says why a source stopped answering; this
    # says what stopped being visible because of it — and they are not the
    # same news. "Rate-limited" alone reads as a temporary annoyance, when the
    # thing worth knowing is that the emails will keep arriving looking normal
    # while resale is dark.
    lost = ", ".join(reading.failed_sources)
    kept = ", ".join(reading.answering_sources) or "nothing"
    if any("browser" in s for s in reading.failed_sources):
        coda = (
            f"The browser source is failing, though {kept} still answers. That "
            "matters more than it looks: the browser is the only source that "
            "can see a Verified Resale listing, which is how a ticket has "
            "actually appeared on this event. The hourly emails will keep "
            "arriving and will keep reporting on primary stock — but on resale "
            "they are now a guess rather than a reading. Treat this as the "
            "watcher being half blind, not merely grumpy."
        )
    else:
        coda = f"The {lost} source is failing; {kept} still answers."

    return f"{cause}\n\n{coda}" if cause else coda


def _maybe_watchdog(reading: Reading, st: dict, failures: int) -> None:
    """Nag if it has been broken for long enough, whether wholly or partly."""
    if not state_mod.should_alert_watchdog(st):
        return

    # Name the page that is actually broken. With more than one being
    # watched, "the watcher has failed 6 checks" leaves you to guess which —
    # and the likeliest cause of one page failing while the other is fine is
    # that page's URL having changed, which is a five-second fix once you
    # know where to look.
    reason = watchdog_reason(reading)
    slug, count = state_mod.worst_event(st)
    if slug and len(config.EVENTS) > 1:
        broken = next((e for e in config.EVENTS if e.slug == slug), None)
        name = broken.name if broken else slug
        healthy = [
            e.name for e in config.EVENTS
            if state_mod.event_state(st, e.slug).get("consecutive_failures", 0) == 0
        ]
        reason += f"\n\nWorst affected: {name} ({count} failed checks in a row)."
        # The URL, not just the name. The likeliest cause of one page failing
        # while the other is fine is that page's URL having changed, and
        # checking that takes seconds once you have the link to open.
        if broken:
            reason += f"\n{broken.url}"
        if healthy:
            reason += "\nStill working: " + ", ".join(healthy) + "."

    notify.watchdog(reason, failures, health=state_mod.connection_health(st))
    st["last_watchdog_alert"] = state_mod.utc_now().isoformat()


def _maybe_heartbeat(reading: Reading, st: dict) -> None:
    if not state_mod.should_send_heartbeat(st):
        return
    hours = state_mod.hours_since_heartbeat(st) or config.HEARTBEAT_HOURS
    checks = st["checks_since_heartbeat"]
    failures = st["failures_since_heartbeat"]
    degraded, resale_blind = state_mod.coverage(st)
    print(
        f"[{stamp()}] hourly report: {checks} checks, {failures} unhealthy "
        f"({degraded} partial), resale unreadable on {resale_blind}"
    )

    # Only meaningful where a browser is running. In API-only mode every poll
    # is structurally resale-blind — the free API cannot see a listing at all
    # — so reporting it hourly would be a constant alarm about a known and
    # accepted limitation rather than news.
    cover = (degraded, resale_blind) if config.USE_BROWSER else None

    # Only talk about networks where there is a network to talk about. In
    # API-only mode this runs on a GitHub runner, where "switch the MacBook
    # to your hotspot" is meaningless and "could not determine which
    # connection is in use" is worse than saying nothing.
    net = state_mod.network_status(st) if config.USE_BROWSER else None

    notify.heartbeat(
        checks, failures, hours, reading,
        health=state_mod.connection_health(st),
        net=net,
        coverage=cover,
        # Every page's own last reading, rather than whichever one happened to
        # trip the clock. Which page a status belongs to is the whole question
        # when two are being watched.
        events=state_mod.event_summaries(st),
    )
    if net and net[0]:
        # Asked for a switch — don't ask again until the next window, whether
        # or not he acts on it. Repeating it hourly trains him to skim past it.
        state_mod.mark_rotation_asked(st)
    state_mod.reset_heartbeat(st)


def run_once(session=None) -> Reading:
    """One full cycle across every watched event.

    Returns the most significant reading — a find beats a block beats a plain
    'nothing', so callers that inspect the result (the watch loop deciding
    whether to back off, or to stop and let David check out) still see the
    thing that matters rather than whichever event happened to be last.

    State is saved once at the end, not per event, so a crash midway cannot
    leave one event's history advanced and the other's behind.
    """
    st = state_mod.load()
    try:
        readings = []
        for reading in poll_all(session):
            handle(reading, st)
            readings.append(reading)
        return _most_significant(readings)
    finally:
        state_mod.save(st)


def _most_significant(readings: list) -> Reading:
    if not readings:
        stub = Reading(source="none")
        stub.failed = True
        return stub
    for found in (r for r in readings if r.any_good):
        return found
    for blocked in (r for r in readings if r.blocked):
        return blocked
    for failed in (r for r in readings if r.failed):
        return failed
    return readings[0]
