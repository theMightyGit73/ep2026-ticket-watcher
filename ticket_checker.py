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
                          just skipped and email still works.

State file (state.json):
  Committed back to the repo by the workflow so the watchdog can track things
  *across* runs. Safe to delete — it regenerates with defaults on the next
  run. New keys added here are backward-compatible with an older state.json
  on disk; no migration step needed.

Known limitation (found 2026-07-01):
  hasEnabledTicketTypes / ticketTypes in __NEXT_DATA__ looks like a
  render-time snapshot, not a live read of the same inventory system the
  actual checkout flow (checkout.ticketmaster.ie/graphql, the `reserve`
  mutation) checks against. A `reserve` call was observed succeeding at
  close to the same moment the rendered page said "aren't enough tickets"
  for this exact event and quantity. So: treat available=True from this
  script as "go check by hand immediately," not "confirmed purchasable."

  This script deliberately does NOT call `reserve` itself. That call rides
  on a live logged-in browser session — not a stable secret suited to an
  unattended cron job — and it's an action against real inventory
  (confirmReserve: true), not a passive read. Automating it on a schedule
  is a materially different, riskier interaction with Ticketmaster than
  quietly reading a public page, and a poor fit for a secret sitting in
  GitHub Actions for weeks unattended.
"""

import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, List, Optional

import cloudscraper
import requests

# ── Config ──────────────────────────────────────────────────────────────────
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


# ── Data shapes ──────────────────────────────────────────────────────────────

@dataclass
class TicketType:
    name: str
    price: str
    locked: bool


@dataclass
class CheckResult:
    available: bool = False
    ticket_types: List[TicketType] = field(default_factory=list)
    method: str = ""
    notes: List[str] = field(default_factory=list)


# ── State persistence ────────────────────────────────────────────────────────

def default_state():
    return {
        "consecutive_failures": 0,
        "watchdog_alert_sent": False,
        "fallback_streak": 0,
        "fallback_alert_sent": False,
        "availability_alert_sent": False,   # don't re-alert every run while still available
        "known_ticket_type_names": [],      # for spotting a new tier appearing
    }


def load_state():
    state = default_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        state.update(saved)
        return state
    except (json.JSONDecodeError, OSError):
        return state


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        print(f"[{utc_now()}] WARNING: could not save state: {exc}")


# ── Fetching ─────────────────────────────────────────────────────────────────

def fetch_page() -> Optional[str]:
    """Fetch the Ticketmaster event page, with a couple of quick retries for
    transient network blips. Returns HTML string or None on total failure.

    Uses cloudscraper rather than plain requests/urllib because Ticketmaster
    IE blocks generic HTTP clients outright — confirmed directly against
    this same URL, a plain fetch gets rejected by bot detection before it
    reaches the page at all."""
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


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_ticket_data(html: str) -> CheckResult:
    """
    Primary: parse the __NEXT_DATA__ JSON blob Ticketmaster (Next.js) embeds
    in every page response.
    Fallback: conservative string matching, used only if the JSON can't be
    found or parsed — e.g. Ticketmaster changed their page structure.
    """
    result = CheckResult()

    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if m:
        try:
            data      = json.loads(m.group(1))
            redux     = data["props"]["initialReduxState"]
            ts        = redux["ticketSelection"]
            page_mode = redux.get("pageMode", {}).get("pageMode", "unknown")

            has_enabled  = ts.get("hasEnabledTicketTypes", False)
            ticket_types = ts.get("ticketTypes", [])
            maintenance  = ts.get("maintenance", False)

            result.method = f"__NEXT_DATA__ JSON (pageMode={page_mode})"

            # Observational only — never alerts. A single returned ticket is
            # most likely to surface as a Verified Resale listing, and it's
            # unconfirmed whether resale flips hasEnabledTicketTypes or lives
            # elsewhere in this blob. Log any resale-flavoured key names so a
            # few days of Actions logs reveal where (or whether) resale data
            # appears in the page payload, before wiring real detection.
            resale_keys = sorted(set(
                re.findall(r'"([^"]*resale[^"]*)"\s*:', m.group(1), re.IGNORECASE)
            ))
            if resale_keys:
                result.notes.append(f"resale-related keys present: {resale_keys}")

            if maintenance:
                result.notes.append("maintenance=true — page temporarily offline")
                return result

            if ticket_types:
                # We don't have confirmed visibility into every field
                # Ticketmaster sends per ticket type (e.g. whether a stable
                # per-tier id or a live-quantity field exists alongside
                # hasEnabledTicketTypes). Log the raw keys so the real
                # schema is visible in the Actions log instead of guessing.
                result.notes.append(f"raw ticket type keys seen: {sorted(ticket_types[0].keys())}")

            if has_enabled and ticket_types:
                result.available = True
                result.notes.append(
                    f"hasEnabledTicketTypes=true · {len(ticket_types)} type(s) found"
                )
                for tt in ticket_types:
                    name   = tt.get("title", "Unknown")
                    prices = tt.get("prices", [])
                    price  = f"€{prices[0]['faceValue']:.2f}" if prices else "price TBC"
                    locked = bool(tt.get("locked"))
                    result.ticket_types.append(TicketType(name=name, price=price, locked=locked))
                    result.notes.append(
                        f"Found: {name} at {price}" + (" [locked — needs code]" if locked else "")
                    )
            else:
                result.notes.append(
                    f"hasEnabledTicketTypes={has_enabled} · ticketTypes count={len(ticket_types)}"
                )

            return result

        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            result.notes.append(f"__NEXT_DATA__ parse failed: {exc} — trying fallback")

    # ── Fallback: conservative string matching ──────────────────────────────
    # Only trusts JSON-shaped signals now, not generic page text. Confirmed
    # directly (2026-07-01) that "Find Tickets" and "General Admission" both
    # appear on the page regardless of real availability — "Find Tickets" is
    # just the search button label, and "General Admission" is baked into
    # the ticket type's own name. Treating either as a "live" signal produces
    # false positives, so they've been dropped rather than fixed — there's no
    # safe substring version of "a button that's always there."
    result.method = "string matching (fallback — JSON parse failed)"

    SOLD_SIGNALS = [
        '"hasenabledtickettypes":false',
        '"hasenabledtickettypes": false',
        "sold out",
        "soldout",
        "eventoffsale",
        "event-offsale",
        "tickets are currently unavailable",
        "enough tickets to complete your request",  # apostrophe-free on purpose — avoids
                                                      # straight/curly quote mismatches
    ]
    LIVE_SIGNALS = [
        '"hasenabledtickettypes":true',
        '"hasenabledtickettypes": true',
    ]

    html_lower = html.lower()
    for s in SOLD_SIGNALS:
        if s in html_lower:
            result.notes.append(f"Sold-out marker: '{s}'")
            return result

    for s in LIVE_SIGNALS:
        if s in html_lower:
            result.available = True
            result.notes.append(f"Available marker: '{s}'")
            return result

    result.notes.append(
        "No clear markers found — defaulting to sold out to avoid false alerts"
    )
    return result


def diff_ticket_types(state: dict, result: CheckResult) -> List[str]:
    """Compare this run's ticket type names against the last known set.
    Returns names that are new since last run. A tier appearing is a useful
    signal even on a run where `available` was already True, since a flat
    boolean can hide a tier-level change (Tier 1 sells out, Tier 2 opens,
    the overall 'enabled' flag never flips)."""
    current_names = [t.name for t in result.ticket_types]
    previous_names = set(state.get("known_ticket_type_names", []))
    new_names = [n for n in current_names if n not in previous_names]
    state["known_ticket_type_names"] = current_names
    return new_names


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
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(f"[{utc_now()}] NTFY_TOPIC not set — skipping push notification")
        return
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


def safe_notify(label: str, fn: Callable, *args, **kwargs):
    """Run a notify call without letting a failure (bad credentials, ntfy
    hiccup, network blip) crash the whole run. Previously notify_watchdog()
    and notify_recovered() weren't guarded like this — a failure in either
    could kill the script before state.json got saved, or in
    notify_recovered()'s case, before the actual ticket check even ran."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        print(f"[{utc_now()}] WARNING: {label} notification failed: {exc}")


def notify_available(ticket_types: List[TicketType], new_names: List[str]):
    lines = [
        f"  • {t.name} — {t.price}" + (" [needs unlock code]" if t.locked else "")
        for t in (ticket_types or [])
    ]
    ticket_block = "\n".join(lines) if lines else "  • (check the site for details)"
    new_line = f"\nNewly appeared since last check: {', '.join(new_names)}\n" if new_names else ""

    subject = f"🎪 TICKETS AVAILABLE: {EVENT_NAME}!"
    body = (
        f"Hi David,\n\n"
        f"Tickets appear to be AVAILABLE for {EVENT_NAME}!\n\n"
        f"What's on sale:\n{ticket_block}\n{new_line}\n"
        f"This is based on the page listing, which can lag the live checkout "
        f"system — go check the page and try to buy right away rather than "
        f"treating this as a guarantee.\n\n"
        f"👉 Book now: {URL}\n\n"
        f"Checked at: {utc_now()}\n\nGood luck! 🤞"
    )
    safe_notify("available-email", send_email, subject, body)
    safe_notify(
        "available-push", send_ntfy,
        title="🎪 EP2026 tickets available!",
        message=f"{ticket_block}\n\nOpen: {URL}",
        priority="urgent",
        tags=["tickets", "rotating_light"],
    )


def notify_watchdog(reason: str):
    subject = "⚠️ EP2026 ticket watcher needs a look"
    body = (
        f"Hi David,\n\n"
        f"The ticket checker has hit a problem: {reason}\n\n"
        f"It'll keep retrying automatically, but you may want to check the "
        f"Actions logs in case Ticketmaster changed something on their end.\n\n"
        f"Checked at: {utc_now()}"
    )
    safe_notify("watchdog-email", send_email, subject, body)
    safe_notify(
        "watchdog-push", send_ntfy,
        title="⚠️ EP2026 watcher problem",
        message=reason,
        priority="high",
        tags=["warning"],
    )


def notify_recovered():
    safe_notify(
        "recovered-push", send_ntfy,
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
    if r.available:
        print("  Status  : ✅  TICKETS AVAILABLE — go go go!")
    else:
        print("  Status  : ❌  Still sold out / unavailable")
    print(f"  Method  : {r.method}")
    for note in r.notes:
        print(f"  Detail  : {note}")
    if r.ticket_types:
        print("  Tickets :")
        for t in r.ticket_types:
            lock = " [needs unlock code]" if t.locked else ""
            print(f"            • {t.name} — {t.price}{lock}")
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
    keep an eye on the checker's own health. State is always saved on the
    way out, even if something above throws — the `finally` is the safety
    net for anything unanticipated; the known failure modes (a notify call
    failing) are now caught explicitly by safe_notify() so they don't reach
    here in the first place."""
    state = load_state()
    try:
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
            return

        # Fetch succeeded — check if we're recovering from a prior outage.
        if state["consecutive_failures"] >= WATCHDOG_FAILURE_THRESHOLD and state["watchdog_alert_sent"]:
            notify_recovered()
        state["consecutive_failures"] = 0
        state["watchdog_alert_sent"] = False

        r = parse_ticket_data(html)
        print(f"  Method : {r.method}")
        for note in r.notes:
            print(f"  Detail : {note}")

        if "fallback" in r.method:
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

        new_names = diff_ticket_types(state, r)

        if r.available:
            if not state["availability_alert_sent"]:
                print(f"[{utc_now()}] 🎉 Tickets available! Sending alerts...")
                notify_available(r.ticket_types, new_names)
                state["availability_alert_sent"] = True
            elif new_names:
                print(f"[{utc_now()}] Still available, and a new tier appeared: {new_names}")
                notify_available(r.ticket_types, new_names)
            else:
                print(f"[{utc_now()}] Still available — already alerted, not re-sending.")
        else:
            print(f"[{utc_now()}] Still sold out.")
            state["availability_alert_sent"] = False

    finally:
        save_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--check" in args:
        cmd_check()
    elif "--test" in args:
        cmd_test()
    else:
        cmd_scheduled()
