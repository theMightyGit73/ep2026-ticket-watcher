"""Email + push delivery.

Lifted from the original watcher, which got this part right: every send is
wrapped so a bad credential or an ntfy hiccup can never take down the run or
lose the state write.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, List

import requests

from . import config
from .model import GOOD_STATUSES, Listing, Reading
from .state import stamp


def _send_email(subject: str, body: str) -> None:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")

    msg = MIMEMultipart()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        srv.send_message(msg)
    print(f"[{stamp()}] Email sent to {config.ALERT_TO}")


def _send_ntfy(title: str, message: str, priority: str = "default", tags=None) -> None:
    if not config.NTFY_TOPIC:
        return
    requests.post(
        f"https://ntfy.sh/{config.NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": ",".join(tags or []),
            "Click": config.EVENT_URL,
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

    subject = f"TICKETS AVAILABLE ({where}): {config.EVENT_NAME}"
    body = (
        f"Hi David,\n\n"
        f"A ticket has shown up for {config.EVENT_NAME} on the {where}.\n\n"
        f"What the watcher saw:\n{_listing_block(reading.listings)}\n{new_block}\n"
        f"Trigger : {reason}\n"
        f"Source  : {reading.source}\n"
        f"Wanted  : {config.WANTED_QUANTITY} ticket(s)\n\n"
        f"Go buy it now — this can be gone in under a minute, and the watcher\n"
        f"deliberately does not buy on your behalf.\n\n"
        f"{config.EVENT_URL}\n\n"
        f"Checked at: {stamp()}\n"
    )
    _safe("available-email", _send_email, subject, body)
    _safe(
        "available-push", _send_ntfy,
        title=f"EP2026: ticket on the {where}",
        message=_listing_block(reading.listings) + "\n\nTap to open Ticketmaster.",
        priority="urgent",
        tags=["tickets", "rotating_light"],
    )


def reserved_in_browser(reading: Reading) -> None:
    """The strongest possible signal: we pressed the button and it worked.

    A successful reserve holds the ticket for a few minutes only, so this
    alert is worded to get David to the already-open browser window fast.
    """
    subject = f"IN THE BASKET — finish checkout now: {config.EVENT_NAME}"
    body = (
        f"Hi David,\n\n"
        f"The watcher pressed 'Find Tickets' and Ticketmaster ACCEPTED it —\n"
        f"{config.WANTED_QUANTITY} ticket(s) are held in a basket right now.\n\n"
        f"{_listing_block(reading.listings)}\n\n"
        f"This hold expires in a couple of minutes. The Chrome window the\n"
        f"watcher used has been left open on the checkout page — go finish it\n"
        f"there, or open the link below and the basket should still be yours.\n\n"
        f"{config.EVENT_URL}\n\n"
        f"Reserved at: {stamp()}\n"
    )
    _safe("reserved-email", _send_email, subject, body)
    _safe(
        "reserved-push", _send_ntfy,
        title="EP2026: TICKET HELD — check out NOW",
        message="A reserve succeeded. The hold expires in minutes.",
        priority="urgent",
        tags=["rotating_light", "shopping_cart"],
    )


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


def heartbeat(checks: int, failures: int, hours: float, reading: Reading,
              health=None, net=None) -> None:
    """The hourly "still nothing, still trying" report.

    Deliberately carries the numbers rather than just the sentiment. "No
    success in the last hour" is compatible with both a healthy watcher and
    one that has been failing every attempt — which is exactly the ambiguity
    the previous watcher died in — so the counts are the point of the email.
    """
    healthy = failures < checks or checks == 0
    health_line = (
        f"Checks run in the last {hours:.1f}h : {checks}\n"
        f"Of those, failed to read the page: {failures}"
    )
    if checks and failures == checks:
        health_line += (
            "\n\nEVERY check failed this hour. That is a broken watcher, not a\n"
            "quiet Ticketmaster — the numbers above are the difference."
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
        else f"No luck yet — still watching {config.EVENT_NAME}"
    )
    body = (
        f"Hi David,\n\n"
        f"No ticket has appeared in the last hour. Still trying.\n\n"
        f"{health_line}\n"
        f"{health_block}\n"
        f"Last reading:\n"
        f"  Box office     : {reading.primary}\n"
        f"  Verified resale: {reading.resale}\n"
        f"  Searching for  : {config.WANTED_QUANTITY} ticket\n\n"
        f"Event page: {config.EVENT_URL}\n\n"
        f"You'll get a separate, much louder email the moment anything shows up.\n"
        f"This one just proves the watcher is still alive.\n\n"
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
        f"Most likely fix: the Ticketmaster login expired. Run\n"
        f"    cd {config.REPO_DIR} && .venv/bin/python -m ep_watcher login\n"
        f"and sign in in the window that opens.\n\n"
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
    subject = f"Watcher stopped — {config.EVENT_NAME}"
    body = (
        f"Hi David,\n\n"
        f"The watcher has reached its stop date ({config.STOP_AFTER_DATE}) and shut\n"
        f"itself down. This is the last email you'll get from it.\n\n"
        f"It ran {checks_total} checks in total.\n\n"
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


def test() -> None:
    """Send one real example of every email the watcher can produce.

    Not just a "credentials work" ping. The alert that matters will arrive
    exactly once, under time pressure, and there is no second chance to
    discover that it went to spam or that the link in it was wrong. So this
    puts all four in the inbox now, while it costs nothing to check them.
    """
    print(f"[{stamp()}] 1/4 connectivity")
    _send_email(
        f"[TEST 1/4] {config.EVENT_NAME} watcher is wired up",
        f"Hi David,\n\nIf you can read this, Gmail credentials work and mail is\n"
        f"reaching you. The next three are samples of the real alerts.\n\n"
        f"At: {stamp()}\n",
    )

    print(f"[{stamp()}] 2/4 availability alert")
    sample = Reading(
        source="test",
        primary="UNAVAILABLE",
        resale="AVAILABLE",
        listings=[Listing(name="Verified Resale — Section STNDN1 (WEEKEND CAMPING)",
                          price="€366.39", kind="resale")],
    )
    available(sample, "TEST — this is what a real find looks like", [])

    print(f"[{stamp()}] 3/4 hourly report")
    heartbeat(
        checks=19, failures=0, hours=1.0,
        reading=Reading(source="test", primary="UNAVAILABLE", resale="UNAVAILABLE"),
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

    print(f"[{stamp()}] 4/4 watchdog")
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
    print(f"\n  Four emails sent to {config.ALERT_TO}.")
    print("  Check they arrived AND that none landed in spam — mark them")
    print("  'not spam' now if they did, not on the day it matters.")
