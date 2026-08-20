"""Orchestration: run the sources, merge their answers, decide who to wake up."""

import time
from typing import List, Optional

from . import config, events, liveness, network, notify, state as state_mod
from .model import GOOD_STATUSES, Reading, better_status
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
        for r in readings:
            merged.notes.extend(f"[{r.source}] {n}" for n in r.notes)
        return merged

    for r in real:
        merged.primary = better_status(merged.primary, r.primary)
        merged.resale = better_status(merged.resale, r.resale)
        merged.listings.extend(r.listings)
    for r in readings:
        merged.notes.extend(f"[{r.source}] {n}" for n in r.notes)
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
    # One line per poll, queryable. The prose above is for reading; this is
    # for answering "what was the gap before each find" without a parser.
    events.emit(
        "poll",
        event=reading.event_slug,
        primary=reading.primary,
        resale=reading.resale,
        source=reading.source,
        failed=reading.failed,
        degraded=reading.degraded,
        blocked=reading.blocked,
        listings=len(reading.listings),
    )

    # Which connection did this go out through? Detected rather than declared,
    # so switching the MacBook's network is all David has to do — the watcher
    # notices by itself and resets its counters.
    if config.USE_BROWSER:
        # Captured before note_network overwrites it — the email has to be
        # able to say what was left, not only what was joined.
        was_key = st.get("current_net") or st.get("current_ip")
        was_ip = st.get("current_ip")
        was_label = state_mod.network_label(st) if was_key else ""
        was_blocks = state_mod.recent_blocks(st, 24, ip=was_key) if was_key else 0
        change = state_mod.note_network(st, network.fingerprint())
        if change:
            _announce_network(st, change, was_ip, was_label, was_blocks)

    if reading.blocked:
        state_mod.record_block(st)
        events.emit("block", event=reading.event_slug,
                    ip=st.get("current_ip"), net=st.get("current_net"))

    # Tell the outside world the Mac is still alive. Every local safeguard
    # assumes the laptop is on; this is the only signal that survives it
    # being shut, flat, or off the network.
    if config.USE_BROWSER:
        liveness.publish(f"poll {st.get('checks_total', 0)} at {stamp()}", state=st)

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
        # The worst this outage ever got, recorded as it happens. The recovery
        # gate cannot reconstruct it afterwards: pages recover one at a time
        # and the global counter falls to the least-broken survivor as each
        # one does, so by the time the last page recovers the counter no
        # longer describes the outage that just ended.
        st["outage_peak_failures"] = max(st.get("outage_peak_failures") or 0, failures)
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
    elif st["consecutive_failures"] == 0 and st.get("outage_started_at"):
        # Every page is healthy again, and an outage was in progress.
        #
        # The gate used to be `was_broken >= THRESHOLD`, reading the global
        # counter at the moment THIS page recovered. That silently stopped
        # working when the pages were given different intervals: the busy page
        # accumulates ~5x the failures of the quiet one, so when it recovers
        # first the global counter drops to the quiet page's much smaller
        # streak. By the time the quiet page recovered and satisfied `== 0`,
        # was_broken was below the threshold and the branch never ran. The
        # 71-minute blackout of 2026-08-18 sent no recovery notice at all, and
        # left outage_started_at set for the next 21 hours — long enough that
        # the following outage would have reported a blackout measured from
        # the previous afternoon.
        #
        # So the peak is now recorded while failing (see record_failure) and
        # read back here, and the clearing is unconditional: whether or not
        # the outage was bad enough to have been announced, it is over, and
        # its bookkeeping must not outlive it.
        peak = st.get("outage_peak_failures") or was_broken
        if peak >= config.WATCHDOG_FAILURE_THRESHOLD:
            notify.recovered(
                peak,
                state_mod.minutes_since(st.get("outage_started_at")),
                st.get("last_failure_reason") or "",
            )
        st["last_watchdog_alert"] = None
        st["last_failure_reason"] = None
        st["outage_started_at"] = None
        st["outage_peak_failures"] = 0

    if not should:
        print(f"[{stamp()}] nothing to report")
        _maybe_heartbeat(reading, st)
        return

    # A reserve that actually succeeded is a different, much more urgent email
    # than "a listing appeared" — there is a live hold with a countdown on it.
    if any("RESERVE ACCEPTED" in n for n in reading.notes):
        notify.reserved_in_browser(reading)
    else:
        # The ordinary alert goes FIRST and unconditionally. Securing is an
        # optimistic extra that takes up to 45 seconds and can fail in a dozen
        # ways; letting it run before the alert would mean a browser problem
        # could delay or swallow the one message this project exists to send.
        notify.available(reading, reason, new_listings)
        hold = _maybe_secure(reading, st)
        if hold is not None and hold.secured:
            # The single most valuable line in this function. A basket lives
            # in the browser this process launched, so anything that restarts
            # the watcher throws the ticket away — and the watchdog restarts a
            # watcher whose poll clock has stopped, which is exactly what a
            # checkout looks like from outside. Writing the hold down is what
            # tells it the difference. See state.note_hold().
            minutes = config.hold_window_minutes(hold.minutes_hint)
            event = next(
                (e for e in config.EVENTS if e.slug == reading.event_slug), None)
            state_mod.note_hold(
                st, minutes,
                event_slug=reading.event_slug,
                priority=getattr(event, "secure_priority", 0),
            )
            print(f"[{stamp()}] hold recorded — nothing will restart the "
                  f"watcher for {minutes:.0f} min")
    # The find itself, with everything needed to reconstruct the race later:
    # which page, how it was seen, what it was, and its Ticketmaster id. Eight
    # finds had to be reassembled from prose on 2026-08-20 to establish that
    # no listing id ever repeats — which is the fact that settled whether
    # these were being sold or merely held in someone else's basket.
    events.emit(
        "find",
        event=reading.event_slug,
        via="sweep" if "sweep" in reading.source else "search",
        reason=reason,
        listings=[l.describe() for l in reading.listings],
        listing_ids=[l.listing_id for l in reading.listings if l.listing_id],
    )
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


def _announce_network(st: dict, change: str, was_ip: str, was_label: str,
                      was_blocks: int) -> None:
    """Log and email that the watcher is now on a different connection.

    Only ever fires once per change: handle() runs per watched page, and by
    the time the second page is handled note_network() has already recorded
    the new connection, so it reports no change.

    `change` is "switched" or "readdressed", decided by whether the default
    gateway changed rather than by comparing labels. The label comparison was
    wrong in both directions — a carrier re-addressing a tether read as a
    switch, and moving from the eir hotspot onto a Sky line on 2026-08-18 read
    as a re-address, because with only two names available both were called
    the same thing.
    """
    now_ip = st.get("current_ip")
    now_label = state_mod.network_label(st)
    # Same connection, new address — the carrier's doing, not David's. Told
    # differently, because "you switched networks" would be a lie, and
    # rate-limited more tightly, because a flapping tether could otherwise
    # send this every few minutes.
    readdressed = change == "readdressed"

    print(
        f"[{stamp()}] network changed — now on {state_mod.describe_network(st)}"
        + (" (new address, same connection)" if readdressed else "")
    )

    if not state_mod.should_email_network(st, readdressed):
        print(f"[{stamp()}] ...re-addressed again within the hour; not mailing about it")
        return

    switch, _, _ = state_mod.network_status(st)
    state_mod.mark_network_emailed(st)
    notify.network_switched(
        now_detail=state_mod.describe_network(st),
        known=state_mod.known_networks(st),
        naming_key=state_mod.naming_key(st),
        named=state_mod.is_named(st),
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


def _maybe_secure(reading: Reading, st: dict = None):
    """Try to hold a resale listing, if that has been switched on.

    Returns the HoldResult when an attempt was made, or None when securing is
    off, the page is watch-only, or there was no resale listing to act on. The
    caller needs the difference: a hold that succeeded has to be written into
    state so the watchdog does not restart the watcher and kill the browser
    the basket lives in.

    Resale only. Primary stock reserves itself as a side effect of the search
    the watcher already does — that is what the RESERVE ACCEPTED path above
    handles — so opening a second signed-in browser for it would be a second
    request for a ticket already held.

    Every failure here is swallowed into an email rather than raised. The
    availability alert has already gone out by the time this runs; nothing
    below it is allowed to break the poll loop.
    """
    if not config.SECURE_ON_FIND:
        return None
    if reading.resale not in GOOD_STATUSES:
        return None

    # Which page is this, and may it be secured?
    #
    # Per page rather than per watcher, because "tell me about it" and "grab
    # it for me" are not the same instruction — see Event.secure. All three
    # pages are securable today. The Early Entry Pass was briefly excluded on
    # the grounds that it is an add-on Ticketmaster only honours alongside a
    # Weekend Ticket, so holding one would pull David to a checkout for
    # something he cannot use on its own; he overruled that on 2026-08-19 and
    # wants it treated as importantly as the ticket. The switch stays because
    # the argument may return, and because a page added later may genuinely
    # not deserve grabbing.
    #
    # Looked up once. This was two identical lookups either side of the
    # listing check, the second of them unreachable in any case the first did
    # not already cover — the kind of duplication that survives because both
    # copies are correct, and then diverges the day one is edited.
    event = next((e for e in config.EVENTS if e.slug == reading.event_slug), None)
    if event is None:
        print(f"[{stamp()}] cannot secure: no event matches {reading.event_slug!r}")
        return None
    if not event.secure:
        print(f"[{stamp()}] {event.slug}: alerting only — this page is not secured")
        return None

    from . import buyer

    listing = next((l for l in reading.listings if l.kind == "resale"), None)
    if listing is None:
        return None

    # Who wins the one buying browser?
    #
    # David's rule, set on 2026-08-19: "weekend ticket is always priority, but
    # try to get the early ticket as well." Both halves are implemented here.
    # The Early Entry Pass is still secured whenever the browser is free —
    # it is worth having — but it gives way, because Ticketmaster only honours
    # it alongside a Weekend Ticket. A held pass while a weekend ticket goes
    # past is the worst available outcome: the one browser spent on the one
    # product that is useless on its own.
    #
    # Preempting drops a certain hold for one that may already be gone. That
    # is the trade the rule chooses, and it is the right way round.
    held = state_mod.held_priority(st) if st is not None else 0
    mine = getattr(event, "secure_priority", 0)
    may_preempt = bool(held) and mine > held
    if held and not may_preempt:
        print(f"[{stamp()}] {event.slug}: a hold of equal or higher importance "
              f"is already live ({st.get('hold_event_slug')}) — leaving it alone")
    elif may_preempt:
        print(f"[{stamp()}] {event.slug} outranks the live hold on "
              f"{st.get('hold_event_slug')} — that hold will be dropped for this")

    print(f"[{stamp()}] listing found — opening the signed-in browser to hold it")
    hold = buyer.secure_in_thread(event, listing, may_preempt=may_preempt,
                                  worker=buy_worker())

    # A dropped hold is news whether or not the swap paid off, and if it did
    # not pay off the record must not keep claiming a ticket is held.
    if hold.preempted and st is not None and not hold.secured:
        state_mod.clear_hold(st)

    # The whole attempt, with its timings. This is the record that makes
    # "did keeping the browser warm help?" a query rather than an argument.
    events.emit(
        "hold",
        event=reading.event_slug,
        secured=hold.secured,
        preempted=hold.preempted,
        reason=hold.reason,
        timings=dict(getattr(hold, "timings", {}) or {}),
        seconds=round(sum((getattr(hold, "timings", {}) or {}).values()), 2),
    )
    if hold.secured:
        print(f"[{stamp()}] HOLD LIVE — browser left open for checkout")
        notify.secured_hold(reading, hold)
    else:
        print(f"[{stamp()}] could not hold it: {hold.reason}")
        notify.secure_failed(reading, hold)
    return hold


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


#: The warm buying browser, when the watch loop has started one.
#:
#: A module-level holder rather than a key in the state dict, and that is not
#: a style preference. state.json is written with json.dump on every cycle, so
#: putting a thread in it would make every save raise TypeError — which
#: state.save() catches and warns about, meaning state would silently stop
#: persisting from the moment the worker was created. The one file the
#: watchdog reads to decide whether a checkout is live is not a place to keep
#: unserialisable objects.
_BUY_WORKER = None


def set_buy_worker(worker) -> None:
    """Tell the engine which warm browser to hand finds to. None disables it."""
    global _BUY_WORKER
    _BUY_WORKER = worker


def buy_worker():
    return _BUY_WORKER


def securing_warning() -> str:
    """Is securing armed but unable to work? One sentence, or "" if it is fine.

    Only ever speaks up on a DEFINITE signed-out reading. session_evidence()
    answers True, False or None, and the None is load-bearing: "cannot tell"
    and "signed out" are different facts, and this project has already been
    bitten by treating the first as the second. A cry-wolf line in every
    hourly email would be read once and skimmed thereafter, which would cost
    exactly the warning this exists to give.

    Read from the cookie database on disk, so it costs no request, no browser
    and no network — and it works while the buying browser is open, which
    matters because the one time it must not throw an error is while a ticket
    is being held in that very profile.

    Checked hourly rather than only at startup because a run lasts a
    fortnight. The account cookies can lapse at any point in it, and the
    startup banner cannot say anything about an hour that began nine days
    later.
    """
    if not config.SECURE_ON_FIND:
        return ""
    from . import buyer

    evidence = buyer.session_evidence(config.BUY_PROFILE_DIR)
    if evidence["signed_in"] is not False:
        return ""
    return (
        "SECURING IS ARMED BUT THE BUYING PROFILE IS SIGNED OUT\n"
        f"({evidence['reason']}).\n"
        "A listing found right now would be alerted on, but not held. Fix it "
        "with:\n    python -m ep_watcher login-buy"
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
        # Armed but signed out is a fault that otherwise stays silent until a
        # real listing is on screen. See securing_warning().
        securing=securing_warning(),
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


class ResaleSweep:
    """Ask the resale endpoint directly between full searches.

    A full search is expensive and slow: a page load, a quantity set, a button
    press, and a wait for a panel that arrives in three stages. That cost is
    what forces the gap between searches out to minutes — and the gap is where
    the tickets are being lost. Measured on 2026-08-20: Weekend Camping was
    searched 30 times at a mean gap of 6.5 minutes, and every completed
    securing attempt arrived to find the listing already gone, including one
    that went from detection to clicking the row in under sixty seconds.

    This asks the one question that matters, as cheaply as it can be asked.
    `fetch_resale_json` runs the fetch inside the live page via
    `page.evaluate`, so it carries that page's cookies, TLS fingerprint and
    origin — Ticketmaster sees the call it already accepts from that tab
    rather than a new client to wall. The endpoint's own response says
    `cache-control: max-age=15`, so being asked every ninety seconds is four
    times politer than the page's own behaviour.

    Deliberately does NOTHING unless it finds something. No counters, no state
    writes, no liveness beacon, no heartbeat arithmetic on an empty sweep —
    those all belong to a real poll, and inflating them every ninety seconds
    would make every health number in the system mean something different. The
    empty case costs exactly one HTTP call and one comparison.

    When it does find a listing it hands off to handle(), so the find is
    alerted, secured, recorded and re-nagged by exactly the same machinery as
    a search-driven find. There is no second alerting path to keep in step.
    """

    #: How many calls between one-line summaries in the log. At 90s x 3 pages
    #: this is roughly half-hourly.
    REPORT_EVERY = 60

    def __init__(self):
        #: slug -> monotonic deadline. In memory rather than in state.json on
        #: purpose: a restart SHOULD sweep immediately, and writing the state
        #: file every ninety seconds to remember something this cheap would be
        #: churn against the one file a crash must not corrupt.
        self._next = {}
        self._refusals = 0
        self.stopped = False
        #: Asked, answered, and came back with nothing to ask with.
        #:
        #: Counted because a sweep that is silently failing looks exactly like
        #: a sweep that is finding nothing, and this project does not allow
        #: those two to share a symptom. The realistic failure is mundane: the
        #: fetch is relative to the page's origin, so a browser parked
        #: somewhere other than ticketmaster.ie returns None every time and
        #: says nothing about it.
        self.calls = 0
        self.answers = 0
        self.unavailable = 0

    def _maybe_report(self) -> None:
        if self.calls and self.calls % self.REPORT_EVERY == 0:
            print(f"[{stamp()}] resale sweep: {self.answers}/{self.calls} calls "
                  f"answered, nothing found on {self.unavailable}")
            events.emit("sweep", calls=self.calls, answers=self.answers,
                        empty=self.unavailable, stopped=self.stopped)

    def due(self, event, now: float) -> bool:
        return now >= self._next.get(event.slug, 0.0)

    def any_due(self, now: float) -> bool:
        """Cheap pre-check for the sleep loop.

        The caller wakes every few seconds; without this it would read and
        parse state.json on every one of those wakes just to discover that
        nothing is due yet. This answers the same question from memory.
        """
        if not config.RESALE_SWEEP or self.stopped:
            return False
        return any(not e.expired() and self.due(e, now) for e in config.EVENTS)

    def _schedule(self, event, now: float) -> None:
        self._next[event.slug] = now + config.RESALE_SWEEP_SECONDS

    def _refused(self, status) -> bool:
        """Did Ticketmaster refuse this call, as opposed to answering it?"""
        return status in (401, 403, 429)

    def run(self, session, st: dict) -> Optional[Reading]:
        """One pass over the due events. Returns the find, or None.

        Never raises. This runs inside the watch loop's sleep window, and
        nothing here is worth costing a poll.
        """
        if not config.RESALE_SWEEP or self.stopped or session is None:
            return None
        # Resting out a 403, or holding a ticket. In the first case the whole
        # point is to stop asking; in the second the watcher has deliberately
        # paused and the buying browser owns the moment.
        if state_mod.backoff_remaining(st) > 0 or state_mod.hold_remaining(st) > 0:
            return None

        now = time.monotonic()
        for event in config.EVENTS:
            if event.expired() or not self.due(event, now):
                continue
            self._schedule(event, now)
            try:
                record = session.fetch_resale_json(event, config.WANTED_QUANTITY)
            except Exception as exc:
                print(f"[{stamp()}] resale sweep: {type(exc).__name__}: {exc}")
                continue
            self.calls += 1
            self._maybe_report()
            if record is None:
                # No answer AND no error to report — the page is very likely
                # not on ticketmaster.ie, so the relative fetch had no origin
                # to resolve against. Counted, so the summary above shows it.
                continue

            status = record.get("status")
            if self._refused(status):
                self._refusals += 1
                # WHICH page was refused is the whole diagnosis, and the first
                # version of this line did not say.
                #
                # On 2026-08-20 the sweep drew three 403s within a minute and
                # shut itself off, while the ordinary searches either side of
                # it succeeded normally — so it was not an IP block, it was
                # that endpoint refusing that call. The leading hypothesis is
                # that the sweep asks for all three event ids from whichever
                # page the browser happens to be parked on, and Ticketmaster
                # objects to being asked about an event whose page you are not
                # on. Two of every three calls are "foreign" in that sense.
                #
                # Recording the slug is what will confirm or kill that: if the
                # refusals cluster on the pages we are NOT parked on, the fix
                # is to sweep only the parked one rather than to slow down.
                print(f"[{stamp()}] resale sweep refused (HTTP {status}) on "
                      f"{event.slug} — {self._refusals}/"
                      f"{config.RESALE_SWEEP_MAX_REFUSALS}")
                events.emit("sweep_refused", event=event.slug, status=status,
                            count=self._refusals)
                if self._refusals >= config.RESALE_SWEEP_MAX_REFUSALS:
                    # Stop for the session. A sweep being refused is not
                    # finding tickets, it is only adding evidence that this
                    # client asks too often — the opposite of its job. The
                    # searches underneath are unaffected and keep running.
                    self.stopped = True
                    print(f"[{stamp()}] resale sweep DISABLED for this session "
                          f"— the searches continue on their own cadence")
                return None
            if record.get("data") is None:
                continue
            self._refusals = 0
            self.answers += 1

            reading = Reading(
                source="resale-sweep",
                event_slug=event.slug,
                event_name=event.name,
                event_url=event.url,
            )
            from .sources.browser import _parse_resale_json

            if not _parse_resale_json(record, reading):
                continue
            if reading.resale not in GOOD_STATUSES:
                # The overwhelmingly common case, and it ends here: no state
                # is written and nothing is sent. Only the local tally moves,
                # so the half-hourly line can prove the sweep is alive.
                self.unavailable += 1
                continue

            # Carry the last known primary forward rather than letting this
            # reading's UNKNOWN overwrite it. record_success() stores whatever
            # the reading holds, and a sweep has no opinion about the box
            # office — reporting one would make the hourly email say the
            # primary status had gone unknown every time a listing appeared.
            reading.primary = state_mod.event_state(st, event.slug).get(
                "last_primary", reading.primary)
            reading.note(
                f"seen by the resale sweep rather than a search — up to "
                f"{config.RESALE_SWEEP_SECONDS}s old, not up to a full search gap"
            )
            print(f"[{stamp()}] resale sweep found something on {event.slug}")
            handle(reading, st)
            return reading
        return None


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
    # A secondary watcher never sends this at all — the cadence change is the
    # same one the primary is already describing, and hearing it twice from
    # two machines is how a useful email becomes one that gets filtered.
    if current.get("mode") and current.get("started_at") and not config.IS_SECONDARY:
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
        # Pages are searched on their own intervals rather than all on every
        # tick. Of the nine resale sightings recorded between 13 and 18
        # August, eight were on the standard page and one on the instalment
        # plan; searching both equally spent half the budget for an eighth of
        # the yield. Weighting them costs no extra requests — see
        # config.searches_per_hour().
        due = state_mod.due_events(st, config.EVENTS)
        # Most important page first. Two reasons, and the second is the one
        # that bites: a cycle can end early — a 403 stops it dead — and
        # whichever page went last is the one that gets skipped. It should
        # never be the weekend ticket. It also means that when listings appear
        # on two pages in the same cycle, the buying browser is spent on the
        # weekend one and the Early Entry pass is the thing that has to give
        # way, rather than the other way round by accident of list order.
        due.sort(key=lambda e: -getattr(e, "secure_priority", 0))
        if not due:
            # Nothing is due. The loop ticks faster than the shortest gap so
            # that a low draw can be honoured, so most ticks land here — this
            # is the normal quiet case, not a fault, and must not be recorded
            # as a check or a failure.
            idle = Reading(source="idle")
            idle.note("no page due yet — waiting out its interval")
            return idle
        for index, event in enumerate(due):
            # Drawn now and stored, so the wait is decided once rather than
            # re-rolled on every tick. See state.note_event_polled().
            state_mod.note_event_polled(st, event.slug, event.next_gap())
            reading = poll(session, event)
            handle(reading, st)
            readings.append(reading)
            # A 403 is a verdict on this client, not on this page. Carrying on
            # to the next one sends another request to an endpoint that has
            # just refused us, earns a second refusal, and books a second
            # resale-blind reading for one wall. Stop and let the caller reset
            # the profile.
            if reading.blocked:
                left = [e.name for e in due[index + 1:]]
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
