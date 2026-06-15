#!/usr/bin/env python3
"""
Electric Picnic 2026 ticket checker.

Commands:
  python3 ticket_checker.py          # Scheduled run: check + email if available
  python3 ticket_checker.py --check  # Manual: print result to terminal, no email needed
  python3 ticket_checker.py --test   # Send a test email to verify credentials
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import cloudscraper

# ── Config ──────────────────────────────────────────────────────────────────
# The exact EP2026 Weekend Camping event page on Ticketmaster IE.
# NOTE: The "Find Tickets" button on this page is client-side only.
# All availability data is pre-loaded into __NEXT_DATA__ JSON on page load —
# we parse that directly, no button click needed.
URL = (
    "https://www.ticketmaster.ie"
    "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
    "/event/18006314BD813D3E"
)
EVENT_NAME = "Electric Picnic 2026 - Weekend Camping"
ALERT_TO   = "davidcoyne73@gmail.com"
# ────────────────────────────────────────────────────────────────────────────


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fetch_page():
    """Fetch the Ticketmaster event page. Returns HTML string or None on failure."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    try:
        resp = scraper.get(URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"[{utc_now()}] Fetch error: {exc}")
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


def send_alert(ticket_types=None, is_test=False):
    """Send an email alert. Requires GMAIL_ADDRESS + GMAIL_APP_PASSWORD env vars."""
    gmail_from = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    if is_test:
        subject = f"TEST: {EVENT_NAME} Ticket Watcher"
        body = (
            "Hi David,\n\n"
            "TEST email — your credentials and script are working perfectly!\n\n"
            f"Checked at: {utc_now()}"
        )
    else:
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

    msg = MIMEMultipart()
    msg["From"]    = gmail_from
    msg["To"]      = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(gmail_from, gmail_pass)
        srv.send_message(msg)
    print(f"[{utc_now()}] ✅ Alert sent to {ALERT_TO}")


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_check():
    """
    --check: fetch the real page and print full availability result.
    No email sent. No environment variables needed.
    """
    print(f"\n[{utc_now()}] Manual check")
    print(f"  URL: {URL}\n")

    html = fetch_page()
    if html is None:
        print("❌  Could not reach Ticketmaster. Check your internet.")
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
    """--test: send a test email to confirm credentials work."""
    if not os.environ.get("GMAIL_ADDRESS") or not os.environ.get("GMAIL_APP_PASSWORD"):
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.")
        sys.exit(1)
    print(f"[{utc_now()}] Sending test email to {ALERT_TO} ...")
    try:
        send_alert(is_test=True)
        print("Done — check your inbox.")
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    sys.exit(0)


def cmd_scheduled():
    """Normal scheduled GitHub Actions run: check and alert if available."""
    print(f"[{utc_now()}] Checking for tickets...")
    html = fetch_page()
    if html is None:
        print("Could not reach Ticketmaster — will retry next run.")
        sys.exit(0)

    r = parse_ticket_data(html)
    print(f"  Method : {r['method']}")
    for note in r["notes"]:
        print(f"  Detail : {note}")

    if r["available"]:
        print(f"[{utc_now()}] 🎉 Tickets available! Sending alert...")
        try:
            send_alert(ticket_types=r["ticket_types"])
        except Exception as exc:
            print(f"ERROR sending email: {exc}")
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
