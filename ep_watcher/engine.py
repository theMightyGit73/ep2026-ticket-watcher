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
    merged.page_gone = any(r.page_gone for r in readings)
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
        readings.append(inventory_api.check(event))

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


def handle(reading: Reading, st: dict) -> None:
    """Fold a reading into state and fire whatever alerts it earns."""
    print(f"[{stamp()}] {reading.summary()}")
    for note in reading.notes:
        print(f"    {note}")
    # What was actually found, not just how many. The log recorded
    # "1 verified-resale listing(s) on the page" and nothing else, so the
    # section and price existed only in the email — and once that is read or
    # deleted there is no way to answer "what was it, and what did it cost?"
    # about a listing that has since sold. A real one appeared on 2026-08-17
    # at 07:49 and lasted about fifteen minutes; the log cannot say what it
    # was. Same shape as `check` prints, so the two read alike.
    for listing in reading.listings:
        print(f"    → {listing.kind}: {listing.describe()}")

    state_mod.start_heartbeat_clock(st)
    # A partial reading counts as unhealthy, not as a clean poll. See
    # Reading.degraded — treating it as clean is what let a blocked browser
    # report "0 failed" for hours.
    # Being refused is never a clean check, even when primary still answered.
    # A resale-endpoint 403 leaves the watcher blind on the only market a
    # ticket has appeared on, and counting that as healthy is what let the
    # 22:18 refusal on 2026-08-17 pass as a spotless poll.
    state_mod.note_check(
        st, unhealthy=reading.failed or reading.degraded or reading.blocked
    )
    if reading.degraded:
        state_mod.note_degraded(st, reading.failed_sources)
    state_mod.note_resale_visibility(st, reading)
    state_mod.note_session_poll(st, reading)
    st["checks_total"] = st.get("checks_total", 0) + 1
    st["last_check_at"] = state_mod.utc_now().isoformat()

    # Which connection did this go out through? Detected rather than declared,
    # so switching the MacBook's network is all David has to do — the watcher
    # notices by itself and resets its counters.
    if config.USE_BROWSER:
        # Captured before note_network overwrites it — the email has to be
        # able to say what was left, not only what was joined.
        was_ip = st.get("current_ip")
        was_label = state_mod.network_label(st) if was_ip else ""
        was_blocks = state_mod.recent_blocks(st, 24, ip=was_ip) if was_ip else 0
        if state_mod.note_network(st, network.public_ip()):
            _announce_network(st, was_ip, was_label, was_blocks)

    if reading.blocked:
        state_mod.record_block(st)

    # Tell the outside world the Mac is still alive. Every local safeguard
    # assumes the laptop is on; this is the only signal that survives it
    # being shut, flat, or off the network.
    if config.USE_BROWSER:
        liveness.publish(f"poll {st.get('checks_total', 0)} at {stamp()}")

    if reading.failed:
        failures = state_mod.record_failure(st, reading)
        # Kept so the recovery email can say what the outage actually was. By
        # the time it is sent the reading is long gone, and "it is working
        # again" without "it had no internet for an hour" is half the story.
        st["last_failure_reason"] = failure_headline(reading)
        # When the blackout began. Not derivable at recovery time: handle()
        # runs per page, so the first page's success moves last_success before
        # the second page gets to measure against it, and the gap reads as
        # zero. Written once, at the start of the run of failures.
        if not st.get("outage_started_at"):
            st["outage_started_at"] = st.get("last_success") or state_mod.utc_now().isoformat()
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
    state_mod.record_success(
        st, reading, healthy=not (reading.degraded or reading.blocked)
    )

    # A partial reading is still a reading: whatever did answer is recorded
    # and alerted on above, exactly as before. What changes is that it no
    # longer clears the failure counter, so a browser that stays blocked now
    # escalates instead of hiding behind the API that is covering for it.
    if reading.degraded or reading.blocked:
        failures = st["consecutive_failures"]
        print(
            f"[{stamp()}] PARTIAL reading — {', '.join(reading.failed_sources)} "
            f"failed ({failures} in a row); still answered by "
            f"{', '.join(reading.answering_sources) or 'nothing'}"
        )
        _maybe_watchdog(reading, st, failures)
    elif was_broken >= config.WATCHDOG_FAILURE_THRESHOLD and st["consecutive_failures"] == 0:
        # Gated on EVERY page being healthy again, not just this one. The
        # global counter tracks the worst event, so handling page one while
        # page two was still broken used to fire this, and handling page two
        # fired it again — two "recovered" emails and two pushes for one
        # recovery, observed on 2026-08-18.
        notify.recovered(
            was_broken,
            state_mod.minutes_since(st.get("outage_started_at")),
            st.get("last_failure_reason") or "",
        )
        st["last_watchdog_alert"] = None
        st["last_failure_reason"] = None
        st["outage_started_at"] = None

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
    # Keep what it was, not just that there was one. By the time the session
    # summary goes out the listing has almost certainly sold, and the count
    # alone cannot tell you what these actually go for.
    state_mod.note_session_find(st, reading)
    # Per event, or alerting on one page would start the re-nag clock for the
    # other and swallow its next find.
    state_mod.event_state(st, reading.event_slug)["last_availability_alert"] = (
        state_mod.utc_now().isoformat()
    )

    # A ticket turned up and David has been told properly. Restart the hourly
    # clock rather than following the good news with "no success this hour".
    state_mod.reset_heartbeat(st)


def _announce_network(st: dict, was_ip: str, was_label: str, was_blocks: int) -> None:
    """Log and email that the watcher is now on a different connection.

    Only ever fires once per change: handle() runs per watched page, and by
    the time the second page is handled note_network() has already recorded
    the new address, so it reports no change.
    """
    now_ip = st.get("current_ip")
    now_label = state_mod.network_label(st)
    # Same connection, new address — the carrier's doing, not David's. Told
    # differently, because "you switched networks" would be a lie, and
    # rate-limited more tightly, because a flapping tether could otherwise
    # send this every few minutes.
    readdressed = bool(was_label) and was_label == now_label

    print(
        f"[{stamp()}] network changed — now on {now_label}"
        + (" (new address, same connection)" if readdressed else "")
    )

    if not state_mod.should_email_network(st, readdressed):
        print(f"[{stamp()}] ...re-addressed again within the hour; not mailing about it")
        return

    switch, _, _ = state_mod.network_status(st)
    state_mod.mark_network_emailed(st)
    notify.network_switched(
        now_label=now_label,
        now_ip=now_ip,
        was_label=was_label or "an unknown connection",
        was_ip=was_ip,
        health=state_mod.connection_health(st),
        was_blocks=was_blocks,
        switch_after=(
            f"You will be asked to switch again after about "
            f"{config.NETWORK_ROTATE_HOURS:.0f}h on this connection, or "
            f"{config.NETWORK_ROTATE_SEARCHES} searches from it."
        ),
        readdressed=readdressed,
    )


#: Fragments that mean "this Mac could not reach the internet", as opposed to
#: "Ticketmaster refused us". Collected from a real outage on 2026-08-18: a
#: power cut took the house network down and every source failed with one of
#: these. The distinction is worth drawing because the two have opposite fixes
#: — one is a router, the other is patience — and because an alert about the
#: network is the one alert the network cannot carry.
_OFFLINE_MARKERS = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_network_changed",
    "err_connection",
    "failed to resolve",
    "nameresolutionerror",
    "max retries exceeded",
    "temporary failure in name resolution",
)


def looks_offline(reading: Reading) -> bool:
    """Did this reading fail because the Mac has no internet at all?"""
    if not reading.failed:
        return False
    joined = " ".join(reading.notes).lower()
    return any(marker in joined for marker in _OFFLINE_MARKERS)


def failure_headline(reading: Reading) -> str:
    """One short phrase for why a reading failed, for the recovery email."""
    if looks_offline(reading):
        return "this Mac had no internet connection"
    if reading.page_gone:
        return "the Ticketmaster event page could not be found"
    if reading.blocked:
        return "Ticketmaster was rate-limiting this client (HTTP 403)"
    if reading.needs_login:
        return "the Ticketmaster session needed a human"
    return "Ticketmaster could not be read"


def watchdog_reason(reading: Reading) -> str:
    """Plain English for why the watcher is unhappy, in priority order.

    The degraded case is worded hardest on purpose. A total failure announces
    itself — the emails stop carrying readings. A partial one does the
    opposite: the hourly report keeps arriving, full of confident-looking
    UNAVAILABLE lines from an API that cannot see resale, while the only
    source that can has been walled for hours. That reads as good news.
    """
    if reading.page_gone:
        cause = (
            "THE EVENT PAGE COULD NOT BE FOUND. Ticketmaster answered 'not "
            "found' rather than serving a page, which almost always means the "
            "URL has changed — pages get reissued when an event is edited. "
            "Nothing will ever be seen on this page until the link is updated. "
            "Open the URL below in a browser: if it fails for you too, search "
            "Ticketmaster for the event and copy the new link into config.py."
        )
    elif looks_offline(reading):
        cause = (
            "THIS MAC CANNOT REACH THE INTERNET. Every source failed to resolve "
            "or connect, which is a local network fault rather than anything to "
            "do with Ticketmaster. Check the Wi-Fi or the hotspot. The watcher "
            "keeps trying and recovers by itself the moment the connection is "
            "back — and it will email you again when it does."
        )
    elif reading.blocked:
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

    # Only start the re-nag clock if the alert actually reached him. When the
    # fault IS the network, this send is exactly what cannot get out, and
    # stamping the clock regardless bought six hours of silence during the
    # 2026-08-18 power cut. An alert nobody received has not been sent.
    if notify.watchdog(reason, failures, health=state_mod.connection_health(st)):
        st["last_watchdog_alert"] = state_mod.utc_now().isoformat()
    else:
        print(
            f"[{stamp()}] could not deliver the watchdog alert — leaving the "
            f"clock unset so the next poll tries again"
        )


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

    delivered = notify.heartbeat(
        checks, failures, hours, reading,
        health=state_mod.connection_health(st),
        net=net,
        coverage=cover,
        # Every page's own last reading, rather than whichever one happened to
        # trip the clock. Which page a status belongs to is the whole question
        # when two are being watched.
        events=state_mod.event_summaries(st),
    )
    if not delivered:
        # The hour is only "reported" once the mail lands. Leaving the clock
        # running means the next poll retries with an honest, longer window
        # rather than throwing the hour away — which is what happened to the
        # 09:22 report during the 2026-08-18 outage.
        print(f"[{stamp()}] hourly report undelivered — will retry on the next poll")
        return
    if net and net[0]:
        # Asked for a switch — don't ask again until the next window, whether
        # or not he acts on it. Repeating it hourly trains him to skim past it.
        state_mod.mark_rotation_asked(st)
    state_mod.reset_heartbeat(st)


def session_settings(to_mode: str) -> list:
    """[(label, before, after)] for what crossing into `to_mode` changes."""
    day_cycle = config.POLL_INTERVAL_SECONDS
    night_cycle = (
        max(config.NIGHT_POLL_SECONDS, day_cycle)
        if config.NIGHT_POLL_SECONDS else day_cycle
    )
    if to_mode == "night":
        cycle_before, cycle_after = day_cycle, night_cycle
        wait_before, wait_after = (
            config.SEARCH_TIMEOUT_SECONDS, config.NIGHT_SEARCH_TIMEOUT_SECONDS)
    else:
        cycle_before, cycle_after = night_cycle, day_cycle
        wait_before, wait_after = (
            config.NIGHT_SEARCH_TIMEOUT_SECONDS, config.SEARCH_TIMEOUT_SECONDS)

    rows = []
    # Only list what genuinely differs. A "changed" line showing the same
    # value on both sides is noise, and it would appear whenever one of the
    # two modes has been configured to match the other.
    if cycle_before != cycle_after:
        rows.append(("Poll cycle", f"every {cycle_before // 60} min",
                     f"every {cycle_after // 60} min"))
    if wait_before != wait_after:
        rows.append(("Search timeout", f"{wait_before}s", f"{wait_after}s"))
    return rows


def _next_switch(to_mode: str) -> str:
    """Plain English for when the settings change back."""
    hour = config.NIGHT_END_HOUR if to_mode == "night" else config.NIGHT_START_HOUR
    other = "daytime" if to_mode == "night" else "overnight"
    # Honest about the imprecision: the cadence is only re-checked after each
    # poll, so the switch lands on the first poll past the hour rather than on
    # it. Saying "07:00" flat would be a small, repeated lie.
    return f"{hour:02d}:00 local (or the first poll after), back to {other}"


def maybe_switch_session(st: dict) -> bool:
    """Close the finished day/night session and open the next one.

    Driven by comparing the stored mode against the current one rather than
    by watching for the moment of transition, so it still fires when the
    watcher was restarted across the boundary — which is exactly when a
    silent switch would be least expected and most confusing.

    Browser mode only. The API-only backstop runs one-shot on a GitHub
    runner, where "your settings have changed" is meaningless and would
    arrive twice a day from a machine that has no cadence to speak of.
    """
    if not config.USE_BROWSER:
        return False

    mode = "night" if config.is_night() else "day"
    current = state_mod.session(st)
    if current.get("mode") == mode:
        return False

    # Nothing to report the very first time: there is no finished session,
    # and an opening email describing zero checks over zero hours is noise.
    if current.get("mode") and current.get("started_at"):
        print(f"[{stamp()}] {current['mode']} session ended — sending the summary")
        notify.session_summary(
            current,
            to_mode=mode,
            hours=state_mod.session_hours(st),
            settings=session_settings(mode),
            next_change=_next_switch(mode),
            health=state_mod.connection_health(st),
            events=state_mod.event_summaries(st),
        )
    state_mod.start_session(st, mode)
    return True


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
        # Before polling, so the finished session's totals stop at the
        # boundary and this cycle counts towards the new one.
        maybe_switch_session(st)
        readings = []
        # Every event is searched on every cycle rather than round-robin, so a
        # listing on the quieter page is never up to a full cycle stale. The
        # cost is paid in the interval instead — see config.poll_interval().
        for event in config.EVENTS:
            reading = poll(session, event)
            handle(reading, st)
            readings.append(reading)
            # A 403 is a verdict on this client, not on this page. Carrying on
            # to the next one sends another request to an endpoint that has
            # just refused us, earns a second refusal, and books a second
            # resale-blind reading for one wall. Stop and let the caller reset
            # the profile.
            if reading.blocked:
                left = [e.name for e in config.EVENTS[len(readings):]]
                if left:
                    print(
                        f"[{stamp()}] blocked — not polling {', '.join(left)} "
                        f"this cycle"
                    )
                break
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
