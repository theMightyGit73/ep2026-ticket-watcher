#!/usr/bin/env python3
"""
Electric Picnic 2026 ticket checker.

Commands:
  python3 ticket_checker.py          # Scheduled run: check + notify if available
  python3 ticket_checker.py --check  # Manual: print result to terminal, no notifications
  python3 ticket_checker.py --test   # Send test notifications (email + ntfy)

Notification channels:
  - Email (Gmail SMTP) — needs GMAIL_ADDRESS + GMAIL_APP_PASSWORD env vars.
  - Push (ntfy.sh)     — needs NTFY_TOPIC env var. Optional: if unset, push is
                          just skipped and email still works. Install the ntfy
                          app, subscribe to a topic name of your choosing
                          (treat it like a password — anyone who knows it can
                          read your alerts), and set that name as NTFY_TOPIC.

State file (state.json):
  Small bit of state committed back to the repo by the workflow so the
  watchdog can track things *across* runs (consecutive failures, whether a
  warning has already been sent, etc). Safe to delete — it'll just
  regenerate with defaults on the next run.
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import cloudscraper
import requests

# ── Config ──────────────────────────────────────────────────────────────────
# The exact EP2026 Weekend Camping event page on Ticketmaster IE.
# NOTE: The "Find Tickets" button on this page is client-side only.
# All availability data is pre-loaded into __NEXT_DATA__ JSON on page load —
# we parse that directly, no button click needed. This is a plain,
# unauthenticated page read — same as a human refreshing the page.
URL = (
    "https://www.ticketmaster.ie"
    "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
    "/event/18006314BD813D3E"
)
EVENT_NAME = "Electric Picnic 2026 - Weekend Camping"
ALERT_TO   = "davidcoyne73@gmail.com"

STATE_FILE = "state.json"
MAX_FETCH_RETRIES = 3
RETRY_DELAY_SECONDS = 5
WATCHDOG_FAILURE_THRESHOLD = 6   # consecutive bad runs before we raise a flag
# ────────────────────────────────────────────────────────────────────────────


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── State persistence (so the watchdog can see across runs) ────────────────

def load_state():
    default = {
        "consecutive_failures": 0,
        "watchdog_alert_sent": False,
        "fallback_streak": 0,
        "fallback_alert_sent": False,
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        default.update(saved)
        return default
    except (json.JSONDecodeError, OSError):
        return default


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        print(f"[{utc_now()}] WARNING: could not save state: {exc}")


# ── Fetching ─────────────────────────────────────────────────────────────────

def fetch_page():
    """Fetch the Ticketmaster event page, with a couple of quick retries for
    transient network blips. Returns HTML string or None on total failure."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    last_exc = None
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            resp = scraper.get(URL, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            print(f"[{utc_now()}] Fetch attempt {attempt}/{MAX_FETCH_RETRIES} failed: {exc}")
            if attempt < MAX_FETCH_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    print(f"[{utc_now()}] Fetch error after {MAX_FETCH_RETRIES} attempts: {last_exc}")
    return None


def parse_ticket_data(html):
    """
    Check availability by parsing the __NEXT_DATA__ JSON blob that
    Ticketmaster (a Next.js app) embeds in every page response.

    When tickets are available:  hasEnabledTicketTypes = true,  ticketTypes = [...]
    When sold out / offsale:     hasEnabledTicketTypes = false, ticketTypes = []

    Returns a dict:
        available    : bool
        ticket_types : list of {name, price}   — only populated when available
        method       : str  — how we determined availability
        notes        : list[str]
    """
    result = {"available": False, "ticket_types": [], "method": "", "notes": []}

    # ── Primary: __NEXT_DATA__ JSON (most reliable) ─────────────────────────
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if m:
        try:
            data       = json.loads(m.group(1))
            redux      = data["props"]["initialReduxState"]
            ts         = redux["ticketSelection"]
            page_mode  = redux.get("pageMode", {}).get("pageMode", "unknown")

            has_enabled  = ts.get("hasEnabledTicketTypes", False)
            ticket_types = ts.get("ticketTypes", [])
            maintenance  = ts.get("maintenance", False)

            result["method"] = f"__NEXT_DATA__ JSON (pageMode={page_mode})"

            if maintenance:
                result["notes"].append("maintenance=true — page temporarily offline")
                return result

            if has_enabled and ticket_types:
                result["available"] = True
                result["notes"].append(
                    f"hasEnabledTicketTypes=true · {len(ticket_types)} type(s) found"
                )
                for tt in ticket_types:
                    name   = tt.get("title", "Unknown")
                    prices = tt.get("prices", [])
                    price  = f"€{prices[0]['faceValue']:.2f}" if prices else "price TBC"
                    locked = " [locked — needs code]" if tt.get("locked") else ""
                    result["ticket_types"].append(
                        {"name": name, "price": price, "locked": bool(tt.get("locked"))}
                    )
                    result["notes"].append(f"Found: {name} at {price}{locked}")
            else:
                result["notes"].append(
                    f"hasEnabledTicketTypes={has_enabled} · ticketTypes count={len(ticket_types)}"
                )

            return result

        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            result["notes"].append(f"__NEXT_DATA__ parse failed: {exc} — trying fallback")

    # ── Fallback: string matching ────────────────────────────────────────────
    result["method"] = "string matching (fallback — JSON parse failed)"

    SOLD_SIGNALS = [
        '"hasEnabledTicketTypes":false',
        '"hasEnabledTicketTypes": false',
        "Sold Out",
        "soldOut",
        "eventOffsale",
        "event-offsale",
        "tickets are currently unavailable",
    ]
    LIVE_SIGNALS = [
        '"hasEnabledTicketTypes":true',
        "Find Tickets",
        "General Admission",
    ]

    for s in SOLD_SIGNALS:
        if s.lower() in html.lower():
            result["notes"].append(f"Sold-out marker: '{s}'")
            return result

    for s in LIVE_SIGNALS:
        if s in html:
            result["available"] = True
            result["notes"].append(f"Available marker: '{s}'")
            return result

    result["notes"].append(
        "No clear markers found — defaulting to sold out to avoid false alerts"
    )
    return result


# ── Notifications ────────────────────────────────────────────────────────────

def send_email(subject, body):
    gmail_from = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart()
    msg["From"]    = gmail_from
    msg["To"]      = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(gmail_from, gmail_pass)
        srv.send_message(msg)
    print(f"[{utc_now()}] ✅ Email sent to {ALERT_TO}")


def send_ntfy(title, message, priority="default", tags=None):
    """Push notification via ntfy.sh. No-ops quietly if NTFY_TOPIC isn't set,
    so this is fully optional and never breaks the email path."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(f"[{utc_now()}] NTFY_TOPIC not set — skipping push notification")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": ",".join(tags or []),
            },
            timeout=10,
        )
        print(f"[{utc_now()}] ✅ Push notification sent")
    except Exception as exc:
        print(f"[{utc_now()}] WARNING: ntfy push failed: {exc}")


def notify_available(ticket_types):
    lines = [
        f"  • {t['name']} — {t['price']}"
        + (" [needs unlock code]" if t.get("locked") else "")
        for t in (ticket_types or [])
    ]
    ticket_block = "\n".join(lines) if lines else "  • (check the site for details)"

    subject = f"🎪 TICKETS AVAILABLE: {EVENT_NAME}!"
    body = (
        f"Hi David,\n\n"
        f"Tickets appear to be AVAILABLE for {EVENT_NAME}!\n\n"
        f"What's on sale:\n{ticket_block}\n\n"
        f"👉 Book now: {URL}\n\n"
        f"Move fast — these go in minutes.\n\n"
        f"Checked at: {utc_now()}\n\nGood luck! 🤞"
    )
    send_email(subject, body)
    send_ntfy(
        title="🎪 EP2026 tickets available!",
        message=f"{ticket_block}\n\nOpen: {URL}",
        priority="urgent",
        tags=["tickets", "rotating_light"],
    )


def notify_watchdog(reason):
    subject = "⚠️ EP2026 ticket watcher needs a look"
    body = (
        f"Hi David,\n\n"
        f"The ticket checker has hit a problem: {reason}\n\n"
        f"It'll keep retrying automatically, but you may want to check the "
        f"Actions logs in case Ticketmaster changed something on their end.\n\n"
        f"Checked at: {utc_now()}"
    )
    send_email(subject, body)
    send_ntfy(
        title="⚠️ EP2026 watcher problem",
        message=reason,
        priority="high",
        tags=["warning"],
    )


def notify_recovered():
    send_ntfy(
        title="✅ EP2026 watcher back to normal",
        message="The ticket checker is working again.",
        priority="low",
        tags=["white_check_mark"],
    )


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_check():
    """--check: fetch the real page and print full availability result. No
    notifications sent. No environment variables needed."""
    print(f"\n[{utc_now()}] Manual check")
    print(f"  URL: {URL}\n")

    html = fetch_page()
    if html is None:
        print("❌  Could not reach Ticketmaster after retries.")
        sys.exit(1)

    r = parse_ticket_data(html)

    print("─" * 64)
    if r["available"]:
        print("  Status  : ✅  TICKETS AVAILABLE — go go go!")
    else:
        print("  Status  : ❌  Still sold out / unavailable")
    print(f"  Method  : {r['method']}")
    for note in r["notes"]:
        print(f"  Detail  : {note}")
    if r["ticket_types"]:
        print("  Tickets :")
        for t in r["ticket_types"]:
            lock = " [needs unlock code]" if t.get("locked") else ""
            print(f"            • {t['name']} — {t['price']}{lock}")
    print("─" * 64)
    print()
    sys.exit(0)


def cmd_test():
    """--test: send test notifications on both channels to confirm credentials work."""
    if not os.environ.get("GMAIL_ADDRESS") or not os.environ.get("GMAIL_APP_PASSWORD"):
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.")
        sys.exit(1)
    print(f"[{utc_now()}] Sending test notifications...")
    try:
        send_email(
            f"TEST: {EVENT_NAME} Ticket Watcher",
            f"Hi David,\n\nTEST email — your credentials and script are working.\n\nChecked at: {utc_now()}",
        )
        send_ntfy(
            title="TEST: EP2026 watcher",
            message="Test push notification — if you see this, ntfy is wired up correctly.",
            priority="default",
            tags=["test_tube"],
        )
        print("Done — check your inbox and phone.")
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    sys.exit(0)


def cmd_scheduled():
    """Normal scheduled GitHub Actions run: check, notify if available, and
    keep an eye on the checker's own health."""
    state = load_state()
    print(f"[{utc_now()}] Checking for tickets...")
    html = fetch_page()

    if html is None:
        state["consecutive_failures"] += 1
        print(f"[{utc_now()}] Fetch failed. Consecutive failures: {state['consecutive_failures']}")
        if (state["consecutive_failures"] >= WATCHDOG_FAILURE_THRESHOLD
                and not state["watchdog_alert_sent"]):
            notify_watchdog(
                f"Couldn't reach Ticketmaster for {state['consecutive_failures']} runs in a row."
            )
            state["watchdog_alert_sent"] = True
        save_state(state)
        sys.exit(0)

    # Fetch succeeded — check if we're recovering from a prior outage.
    if state["consecutive_failures"] >= WATCHDOG_FAILURE_THRESHOLD and state["watchdog_alert_sent"]:
        notify_recovered()
    state["consecutive_failures"] = 0
    state["watchdog_alert_sent"] = False

    r = parse_ticket_data(html)
    print(f"  Method : {r['method']}")
    for note in r["notes"]:
        print(f"  Detail : {note}")

    # Watchdog: flag it if we've silently fallen back to the less-reliable
    # string-matching parser several runs in a row — usually means
    # Ticketmaster changed their page structure and the JSON path needs updating.
    if "fallback" in r["method"]:
        state["fallback_streak"] += 1
    else:
        state["fallback_streak"] = 0
        state["fallback_alert_sent"] = False

    if state["fallback_streak"] >= WATCHDOG_FAILURE_THRESHOLD and not state["fallback_alert_sent"]:
        notify_watchdog(
            "The __NEXT_DATA__ JSON parsing has failed and fallen back to string "
            "matching for several runs in a row — Ticketmaster may have changed "
            "their page structure. Worth a look."
        )
        state["fallback_alert_sent"] = True

    save_state(state)

    if r["available"]:
        print(f"[{utc_now()}] 🎉 Tickets available! Sending alerts...")
        try:
            notify_available(r["ticket_types"])
        except Exception as exc:
            print(f"ERROR sending notifications: {exc}")
            sys.exit(1)
    else:
        print(f"[{utc_now()}] Still sold out.")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--check" in args:
        cmd_check()
    elif "--test" in args:
        cmd_test()
    else:
        cmd_scheduled()
