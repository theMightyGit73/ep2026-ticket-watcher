"""Email + push delivery.

Lifted from the original watcher, which got this part right: every send is
wrapped so a bad credential or an ntfy hiccup can never take down the run or
lose the state write.
"""

import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, List

import requests

from . import config
from .model import GOOD_STATUSES, UNKNOWN, Listing, Reading
from .state import stamp


#: Set while sending sample alerts, so every one of them is unmistakably a
#: drill. Without this a sample reads exactly like the real thing — a test
#: "watcher stopped" email once landed saying "this is the last email you'll
#: get from it" while the watcher was running perfectly well. An alert you
#: cannot tell from a rehearsal is worse than no rehearsal.
TEST_MODE = False


def _mark(text: str) -> str:
    return f"[TEST — not real] {text}" if TEST_MODE else text


def _send_email(subject: str, body: str) -> None:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    subject = _mark(subject)

    msg = MIMEMultipart()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        srv.send_message(msg)
    print(f"[{stamp()}] Email sent to {config.ALERT_TO}")


def _send_ntfy(title: str, message: str, priority: str = "default", tags=None,
               click: str = None) -> None:
    """`click` is where tapping the notification takes you.

    Defaulting it to the first event was fine with one page; with two it
    would open the wrong one, at speed, while the real listing sold.
    """
    if not config.NTFY_TOPIC:
        return
    requests.post(
        f"https://ntfy.sh/{config.NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": _mark(title),
            "Priority": priority,
            "Tags": ",".join(tags or []),
            "Click": click or config.EVENT_URL,
        },
        timeout=10,
    )
    print(f"[{stamp()}] Push sent")


def _safe(label: str, fn: Callable, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        print(f"[{stamp()}] WARNING: {label} notification failed: {exc}")


# ── The alerts ───────────────────────────────────────────────────────────────

def _event_of(reading: Reading) -> tuple:
    """(name, url) for the page a reading is about. Never guessed from config.

    Two pages are watched — an ordinary Weekend Camping ticket and the
    Weekend Camping Instalment Plan — and they are separate products with
    separate inventory. An alert that takes its name and link from
    config.EVENT_* always describes the first one, so a find on the instalment
    page would send David to a page with nothing on it while the real listing
    sold. The config values remain only as the fallback for a reading that
    genuinely carries no event, which is the single-event and test case.
    """
    name = getattr(reading, "event_name", "") or config.EVENT_NAME
    url = getattr(reading, "event_url", "") or config.EVENT_URL
    return name, url


def _which_ticket(name: str) -> str:
    """The one-line distinction between the two pages, spelled out.

    Both are "Weekend Camping" and the names differ only by a trailing
    "Instalment Plan", which is easy to skim past on a phone at speed. Since
    the two cost differently and are bought differently, the email says which
    kind it is in its own words rather than relying on the reader spotting a
    suffix.
    """
    if "instalment" in (name or "").lower():
        return ("This is the INSTALMENT PLAN page — the pay-in-stages listing, "
                "not the standard one.")
    return ("This is the STANDARD Weekend Camping page — the pay-in-full "
            "listing, not the instalment plan.")


def _listing_block(listings: List[Listing]) -> str:
    if not listings:
        return "  (the source reported availability but named no specific tier)"
    return "\n".join(f"  • {l.describe()}" for l in listings)


def available(reading: Reading, reason: str, new_listings: List[str]) -> None:
    which = []
    if reading.primary in GOOD_STATUSES:
        which.append("box office")
    if reading.resale in GOOD_STATUSES:
        which.append("verified resale")
    where = " and ".join(which) or "Ticketmaster"

    new_block = ""
    if new_listings:
        new_block = "\nNew since the last check:\n" + "\n".join(f"  • {n}" for n in new_listings) + "\n"

    # Name the event from the reading, never from config: with more than one
    # page being watched, an alert that says the wrong one sends you to a page
    # with nothing on it while the real listing sells.
    name, url = _event_of(reading)

    subject = f"TICKETS AVAILABLE ({where}): {name}"
    body = (
        f"Hi David,\n\n"
        f"A ticket has shown up for {name} on the {where}.\n\n"
        f"{_which_ticket(name)}\n\n"
        f"What the watcher saw:\n{_listing_block(reading.listings)}\n{new_block}\n"
        f"Trigger : {reason}\n"
        f"Source  : {reading.source}\n"
        f"Wanted  : {config.WANTED_QUANTITY} ticket(s)\n\n"
        f"Go buy it now — this can be gone in under a minute, and the watcher\n"
        f"deliberately does not buy on your behalf.\n\n"
        f"{url}\n\n"
        f"Checked at: {stamp()}\n"
    )
    _safe("available-email", _send_email, subject, body)
    _safe(
        "available-push", _send_ntfy,
        title=f"EP2026: ticket on the {where}",
        message=f"{name}\n\n" + _listing_block(reading.listings)
                + "\n\nTap to open this event.",
        priority="urgent",
        tags=["tickets", "rotating_light"],
        click=url,
    )


def reserved_in_browser(reading: Reading) -> None:
    """The strongest possible signal: we pressed the button and it worked.

    A successful reserve holds the ticket for a few minutes only, so this
    alert is worded to get David to the already-open browser window fast.

    Every identifying detail comes from the reading. This alert used to read
    them from config, which meant the loudest, most time-critical message the
    watcher can send — the one with a countdown on it — always named the
    standard Weekend Camping page and linked to it, whichever page had
    actually reserved. available() was fixed for that; this was not, and the
    only real find so far was on the instalment page.
    """
    name, url = _event_of(reading)

    subject = f"IN THE BASKET — finish checkout now: {name}"
    body = (
        f"Hi David,\n\n"
        f"The watcher pressed 'Find Tickets' and Ticketmaster ACCEPTED it —\n"
        f"{config.WANTED_QUANTITY} ticket(s) are held in a basket right now.\n\n"
        f"Event: {name}\n"
        f"{_which_ticket(name)}\n\n"
        f"{_listing_block(reading.listings)}\n\n"
        f"This hold expires in a couple of minutes. The Chrome window the\n"
        f"watcher used has been left open on the checkout page — go finish it\n"
        f"there, or open the link below and the basket should still be yours.\n\n"
        f"{url}\n\n"
        f"Reserved at: {stamp()}\n"
    )
    _safe("reserved-email", _send_email, subject, body)
    _safe(
        "reserved-push", _send_ntfy,
        title="EP2026: TICKET HELD — check out NOW",
        message=f"{name}\n\nA reserve succeeded. The hold expires in minutes.",
        priority="urgent",
        tags=["rotating_light", "shopping_cart"],
        click=url,
    )


def _status_word(status: str) -> str:
    """Spell out UNKNOWN, which is the one status that reads as reassuring.

    "UNAVAILABLE" is a real answer and looks like one. "UNKNOWN" sitting in
    the same column looks like a third flavour of no, when it actually means
    nothing could read that market at all — which is the state most likely to
    cost the ticket, and so the one that must not read as calm.
    """
    if status == UNKNOWN:
        return "UNKNOWN — nothing could read this market"
    return status


def _health_section(health) -> str:
    """Render the connection-health block that goes into status emails.

    Deliberately always present, including when everything is fine. The point
    is that David can tell at a glance whether his connection is in trouble
    without having to infer it from a run of quiet failures — which is exactly
    what he had to do the day his home IP got flagged.
    """
    severity, headline, action = health
    marker = {"ok": "OK", "watch": "WATCH", "blocked": "BLOCKED"}[severity]
    return (
        f"Connection health [{marker}]\n"
        f"  {headline}\n\n"
        f"  {action}"
    )


def _network_section(net) -> str:
    """Render the "which connection, and should you switch" block."""
    should_switch, headline, instruction = net
    marker = "SWITCH NOW" if should_switch else "OK"
    body = f"Network [{marker}]\n  {headline}"
    if instruction:
        body += f"\n\n{instruction if should_switch else '  ' + instruction}"
    return body


def _reading_block(events, reading: Reading) -> str:
    """What every watched page last said.

    The hourly report used to print whichever single reading happened to
    trigger it, beneath a link hardcoded to the first event — so a report
    could show the instalment plan's statuses under the standard page's URL,
    and there was no way to tell from the email which one it meant. It now
    reports both pages, from each page's own history, every hour.

    `events` is a list of (name, url, primary, resale) from
    state.event_summaries(). Without it this falls back to the single reading,
    which is the API-only and single-event case.
    """
    if not events:
        name, url = _event_of(reading)
        return (
            f"Last reading — {name}\n"
            f"  Box office     : {_status_word(reading.primary)}\n"
            f"  Verified resale: {_status_word(reading.resale)}\n"
            f"  {url}"
        )

    lines = ["Last reading, page by page:"]
    for name, url, primary, resale in events:
        lines.append(
            f"\n  {name}\n"
            f"    Box office     : {_status_word(primary)}\n"
            f"    Verified resale: {_status_word(resale)}\n"
            f"    {url}"
        )
    return "\n".join(lines)


def heartbeat(checks: int, failures: int, hours: float, reading: Reading,
              health=None, net=None, coverage=None, events=None) -> None:
    """The hourly "still nothing, still trying" report.

    Deliberately carries the numbers rather than just the sentiment. "No
    success in the last hour" is compatible with both a healthy watcher and
    one that has been failing every attempt — which is exactly the ambiguity
    the previous watcher died in — so the counts are the point of the email.

    `coverage` is (degraded, resale_blind) and answers the harder question:
    not "did the watcher run" but "could it see". A poll that ran fine and
    learned nothing about resale is the one that quietly costs the ticket.
    """
    healthy = failures < checks or checks == 0

    def row(label, value):
        return f"{label:<30}: {value}"

    # Say what is being counted. Every watched page is polled on every cycle
    # and counted separately, so with two pages this number is twice the
    # number of cycles — which reads as a suspiciously busy watcher unless
    # the email says so.
    pages = len(events) if events else len(config.EVENTS)
    across = f" (across {pages} pages)" if pages > 1 else ""
    health_line = "\n".join([
        row(f"Page checks in the last {hours:.1f}h", f"{checks}{across}"),
        row("Of those, unhealthy", failures),
    ])
    if coverage:
        degraded, resale_blind = coverage
        health_line += "\n" + "\n".join([
            row("  · partial (a source failed)", degraded),
            row("  · resale could not be read", resale_blind),
        ])
        if checks and resale_blind == checks:
            health_line += (
                "\n\nRESALE WAS UNREADABLE ON EVERY CHECK THIS HOUR. Primary stock\n"
                "is still being read, so the watcher looks fine — but resale is\n"
                "the market a ticket has actually turned up on, and right now it\n"
                "is dark. Worth running doctor."
            )
        elif checks and resale_blind > checks / 2:
            health_line += (
                f"\n\nResale was unreadable on {resale_blind} of {checks} checks — over half.\n"
                "The searches are resolving before the resale panel renders."
            )
    if checks and failures == checks:
        health_line += (
            "\n\nEVERY check was unhealthy this hour. That is a broken watcher,\n"
            "not a quiet Ticketmaster — the numbers above are the difference."
        )

    health_block = f"\n{_health_section(health)}\n" if health else ""
    net_block = f"\n{_network_section(net)}\n" if net else ""
    if net_block:
        health_block += net_block

    # Put the ask in the subject line: a "switch networks" instruction buried
    # three paragraphs into an hourly "no luck yet" email is one nobody reads.
    switch_now = bool(net and net[0])
    subject = (
        "Switch the MacBook to your other network — EP2026 watcher"
        if switch_now
        else f"No luck yet — still watching {config.WATCH_LABEL}"
    )
    body = (
        f"Hi David,\n\n"
        f"No ticket has appeared in the last hour. Still trying.\n\n"
        f"{health_line}\n"
        f"{health_block}\n"
        f"{_reading_block(events, reading)}\n\n"
        f"Searching for {config.WANTED_QUANTITY} ticket(s) on each page.\n\n"
        f"You'll get a separate, much louder email the moment anything shows up,\n"
        f"naming which of the pages above it is. This one just proves the\n"
        f"watcher is still alive.\n\n"
        f"Checked at: {stamp()}\n"
    )
    _safe("heartbeat-email", _send_email, subject, body)
    if not healthy:
        _safe(
            "heartbeat-push", _send_ntfy,
            title="EP2026 watcher: every check failing",
            message=f"{failures}/{checks} checks failed this hour.",
            priority="high",
            tags=["warning"],
        )


def mac_watcher_silent(hours: float) -> None:
    """Sent from GitHub when the Mac has stopped checking in.

    This is the alert nothing on the Mac could ever send, because by
    definition the Mac is the thing that stopped. It goes out from GitHub's
    infrastructure, which is why it survives a shut lid, a flat battery or a
    dropped Wi-Fi connection.
    """
    subject = "EP2026: your Mac watcher has gone quiet"
    body = (
        f"Hi David,\n\n"
        f"The watcher on your MacBook has not checked in for {hours:.1f} hours.\n"
        f"This message comes from GitHub, not from the Mac — which is the point:\n"
        f"if the laptop is shut, flat, or off the network, nothing on it could\n"
        f"tell you.\n\n"
        f"The GitHub backstop is still running, but it can only see a coarse\n"
        f"re-release. It cannot see a Verified Resale listing, which is how a\n"
        f"ticket has actually appeared so far. So right now you have much less\n"
        f"cover than you think.\n\n"
        f"To fix, on the MacBook:\n\n"
        f"  1. Wake it, and make sure it is on Wi-Fi or the hotspot.\n"
        f"  2. cd {config.REPO_DIR} && ./run_watcher.sh doctor\n"
        f"  3. If anything is wrong:  ./restart.sh\n\n"
        f"You should see this stop within about 15 minutes of the watcher\n"
        f"running again.\n\n"
        f"Noticed at: {stamp()}\n"
    )
    _safe("mac-silent-email", _send_email, subject, body)
    _safe(
        "mac-silent-push", _send_ntfy,
        title="EP2026: Mac watcher is down",
        message=f"No check-in for {hours:.1f}h. The sharp watcher is not running.",
        priority="high",
        tags=["warning"],
    )


def watchdog(reason: str, failures: int, health=None) -> None:
    health_block = f"\n{_health_section(health)}\n" if health else ""

    subject = "EP2026 watcher is not working"
    body = (
        f"Hi David,\n\n"
        f"The ticket watcher has failed {failures} checks in a row.\n\n"
        f"Reason: {reason}\n"
        f"{health_block}\n"
        f"It will keep retrying, and it will keep nagging you every "
        f"{config.WATCHDOG_RENAG_HOURS}h until it recovers, so you can trust "
        f"silence to mean 'working'.\n\n"
        f"To find out what is actually wrong, and get the exact fix:\n\n"
        f"    {config.REPO_DIR}/run_watcher.sh doctor\n\n"
        f"To just put everything back the way it should be:\n\n"
        f"    {config.REPO_DIR}/restart.sh\n\n"
        f"Both are safe to run as often as you like.\n\n"
        f"Checked at: {stamp()}\n"
    )
    _safe("watchdog-email", _send_email, subject, body)
    _safe(
        "watchdog-push", _send_ntfy,
        title="EP2026 watcher is broken",
        message=reason,
        priority="high",
        tags=["warning"],
    )


def stopped(checks_total: int) -> None:
    """Final email: the watcher has reached its stop date and shut down.

    Sent once, on the way out. Without it the watcher simply goes silent, and
    silence is the one thing this whole design refuses to be ambiguous about —
    "no more emails" should never leave you wondering whether it died or
    finished.
    """
    subject = f"Watcher stopped — {config.WATCH_LABEL}"
    pages = "\n".join(f"  · {e.name}" for e in config.EVENTS)
    body = (
        f"Hi David,\n\n"
        f"The watcher has reached its stop date ({config.STOP_AFTER_DATE}) and shut\n"
        f"itself down. This is the last email you'll get from it.\n\n"
        f"It ran {checks_total} checks in total, across:\n{pages}\n\n"
        f"Nothing is left running on a schedule. If you want to tidy up:\n\n"
        f"  launchctl unload ~/Library/LaunchAgents/com.davidcoyne.ep2026watcher.plist\n"
        f"  sudo pmset -a disablesleep 0\n\n"
        f"To watch a later event, set EP_STOP_AFTER to a new date in\n"
        f"~/.ep2026-watcher/env and start it again.\n\n"
        f"Stopped at: {stamp()}\n"
    )
    _safe("stopped-email", _send_email, subject, body)
    _safe(
        "stopped-push", _send_ntfy,
        title="EP2026 watcher stopped",
        message="Reached its stop date and shut down. No further alerts.",
        priority="low",
        tags=["checkered_flag"],
    )


def recovered(after: int) -> None:
    _safe(
        "recovered-push", _send_ntfy,
        title="EP2026 watcher recovered",
        message=f"Back to normal after {after} failed checks.",
        priority="low",
        tags=["white_check_mark"],
    )
    _safe(
        "recovered-email", _send_email,
        "EP2026 watcher is working again",
        f"Hi David,\n\nThe watcher recovered after {after} failed checks and is "
        f"reading Ticketmaster normally again.\n\nAt: {stamp()}\n",
    )


def verify_push() -> tuple:
    """Publish a silent message and read it back. Returns (ok, detail).

    Proves the ntfy topic is live and reachable end to end, rather than
    assuming a POST that returned 200 arrived anywhere. It cannot prove the
    phone is subscribed — nothing server-side can — so the detail line says
    what was and was not established, instead of implying more than it knows.

    Priority 1 and no tags, so this never buzzes a phone that IS subscribed.
    """
    if not config.NTFY_TOPIC:
        return False, "NTFY_TOPIC not set — no push notifications at all"

    marker = f"selfcheck-{int(time.time())}"
    try:
        resp = requests.post(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=marker.encode("utf-8"),
            headers={"Title": "EP2026 self-check", "Priority": "1", "Tags": "gear"},
            timeout=15,
        )
        if resp.status_code != 200:
            return False, f"ntfy rejected the publish (HTTP {resp.status_code})"
    except requests.RequestException as exc:
        return False, f"could not reach ntfy.sh: {exc}"

    # Read it back from the topic's cache. Not instantly: ntfy accepts the
    # publish before the message is queryable, so reading with no pause
    # reports a perfectly working topic as broken — which it did, the first
    # time this ran. Retry briefly rather than trusting one attempt.
    last = "no response"
    for delay in (1.5, 2.5, 4.0):
        time.sleep(delay)
        try:
            got = requests.get(
                f"https://ntfy.sh/{config.NTFY_TOPIC}/json",
                params={"poll": "1", "since": "120s"},
                timeout=15,
            )
            if marker in got.text:
                return True, f"published and read back from topic {config.NTFY_TOPIC}"
            last = "published, but the message did not appear in the topic"
        except requests.RequestException as exc:
            last = f"published, but read-back failed: {exc}"
    return False, last


def test() -> None:
    """Send one real example of every email the watcher can produce.

    Not just a "credentials work" ping. The alert that matters will arrive
    exactly once, under time pressure, and there is no second chance to
    discover that it went to spam or that the link in it was wrong. So this
    puts all four in the inbox now, while it costs nothing to check them.
    """
    global TEST_MODE
    TEST_MODE = True

    pages = "\n".join(f"  · {e.name}\n    {e.url}" for e in config.EVENTS)
    print(f"[{stamp()}] 1/5 connectivity")
    _send_email(
        f"{config.WATCH_LABEL} watcher is wired up",
        f"Hi David,\n\nIf you can read this, Gmail credentials work and mail is\n"
        f"reaching you. The next four are samples of the real alerts.\n\n"
        f"Watching {len(config.EVENTS)} page(s):\n{pages}\n\n"
        f"At: {stamp()}\n",
    )

    # Deliberately the LAST configured page — the instalment plan, if there is
    # one. Every alert used to describe the first event whatever had actually
    # been found, so a drill built on the first event proved nothing. This one
    # is wrong the moment that regresses.
    found_on = config.EVENTS[-1]

    print(f"[{stamp()}] 2/5 availability alert")
    sample = Reading(
        source="test",
        event_slug=found_on.slug,
        event_name=found_on.name,
        event_url=found_on.url,
        primary="UNAVAILABLE",
        resale="AVAILABLE",
        listings=[Listing(name="Verified Resale — Section STNDN1 (WEEKEND CAMPING)",
                          price="€366.39", kind="resale")],
    )
    available(sample, "TEST — this is what a real find looks like", [])

    print(f"[{stamp()}] 3/5 basket alert")
    # The loudest one the watcher can send, and the one that arrives with a
    # checkout timer already running. Worth seeing once in advance.
    reserved_in_browser(
        Reading(
            source="test",
            event_slug=found_on.slug,
            event_name=found_on.name,
            event_url=found_on.url,
            primary="AVAILABLE",
            listings=[Listing(name="General Admission (in basket)",
                              price="€310.50", kind="primary")],
        )
    )

    print(f"[{stamp()}] 4/5 hourly report")
    heartbeat(
        checks=12, failures=0, hours=1.0,
        reading=Reading(source="test", primary="UNAVAILABLE", resale="UNAVAILABLE"),
        # Shown exactly as the real hourly report renders it: every watched
        # page, with its own statuses and its own link.
        events=[
            (e.name, e.url, "UNAVAILABLE", "UNAVAILABLE" if i == 0 else "UNKNOWN")
            for i, e in enumerate(config.EVENTS)
        ],
        health=("ok", "No blocks in the last 24 hours — this connection looks healthy.",
                "Nothing to do."),
        net=(
            True,
            "On home Wi-Fi (86.44.208.194) — 61 searches over 6.2h.",
            "TIME TO SWITCH NETWORKS — 6.2h on this connection and 61 searches from it.\n\n"
            "  Move the MacBook from home Wi-Fi to your phone hotspot.\n\n"
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  iPhone's Personal Hotspot. (On the phone: Settings > Personal Hotspot.)\n\n"
            "  Nothing else to do. The watcher notices the new connection by itself,\n"
            "  resets its counters, and will tell you when to switch back.",
        ),
    )

    print(f"[{stamp()}] 5/5 watchdog")
    # Sent with the worst-case health block on purpose: this is the email that
    # has to be useful on the day the connection is actually in trouble, so
    # the sample should show the full set of instructions, not the calm case.
    watchdog(
        "TEST — this is what a broken watcher looks like.", failures=4,
        health=(
            "blocked",
            "8 blocks in the last hour (23 in 24h) — this connection is blocked.",
            "Act on this one:\n"
            "  1. Stop the watcher. Repeated attempts extend the block.\n"
            "  2. To browse or buy right now, switch to mobile data.\n"
            "  3. Sign in to your Ticketmaster account.\n"
            "  4. Leave it a few hours — these blocks decay on their own.\n"
            "  5. Raise EP_POLL_SECONDS before restarting.",
        ),
    )

    _send_ntfy(
        title="TEST: EP2026 watcher",
        message="Test push — ntfy is wired up.",
        tags=["test_tube"],
    )
    TEST_MODE = False
    print(f"\n  Five emails sent to {config.ALERT_TO}, each subject prefixed")
    print("  '[TEST — not real]' so you can tell them from the genuine article.")
    print("  Check they arrived AND that none landed in spam — mark them")
    print("  'not spam' now if they did, not on the day it matters.")
    print()
    print(f"  The find and basket samples are about: {found_on.name}")
    print("  Check they name and link THAT page — the two pages differ only by")
    print("  a suffix, and an alert pointing at the wrong one costs the ticket.")
