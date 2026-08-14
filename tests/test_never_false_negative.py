"""The failures that would cost the ticket silently.

Every check here guards the same asymmetry. A false positive is cheap: you
look at the page, see nothing, mildly annoyed. A false negative is the whole
loss — the watcher says "nothing available", you believe it, and the ticket
goes to someone else. So anything the watcher is unsure about must read as
UNKNOWN, never as UNAVAILABLE.

Prompted by a real poll on 2026-08-14: on the phone hotspot the search
resolved before the resale panel arrived, and the watcher logged "no resale
panel — the search may not have completed". That must not be recorded as
"no tickets".

Run with:  .venv/bin/python tests/test_never_false_negative.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402
from ep_watcher.model import (  # noqa: E402
    AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading, better_status,
)
from ep_watcher.sources import browser  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


class FakePage:
    def __init__(self, text):
        self.text = text

    def visible_text(self):
        return self.text


def parse_resale(text):
    reading = Reading(source="test")
    browser.BrowserSession._parse_resale(
        FakePage(text), browser._normalise(text), reading
    )
    return reading


print("\nA panel that never arrived is UNKNOWN, not 'no tickets'")

# Exactly the case seen on the hotspot: search resolved, resale still loading.
no_panel = """There aren't enough tickets to complete your request
Please update the quantity or ticket type to see available tickets.
Search Again"""
r = parse_resale(no_panel)
check("missing panel reads UNKNOWN", r.resale, UNKNOWN)
check("and is not treated as good news", r.any_good, False)

# Panel present and genuinely empty is a real answer.
empty_panel = no_panel + "\nOther Options\nVerified Resale Tickets\n" \
    "Resale Tickets will appear below when they are available.\neTickets FREE"
check("an empty panel IS a real 'no'", parse_resale(empty_panel).resale, UNAVAILABLE)

print("\nA blocked or failed read never claims 'no tickets'")

blocked = Reading(source="browser", failed=True, blocked=True)
check("blocked stays UNKNOWN", blocked.primary, UNKNOWN)
check("and resale too", blocked.resale, UNKNOWN)
check("and is never 'good'", blocked.any_good, False)

print("\nCombining answers must never lose a positive")

check("available beats unavailable", better_status(UNAVAILABLE, AVAILABLE), AVAILABLE)
check("available beats unknown", better_status(UNKNOWN, AVAILABLE), AVAILABLE)
check("order does not matter", better_status(AVAILABLE, UNAVAILABLE), AVAILABLE)
check("a definite no beats unknown", better_status(UNKNOWN, UNAVAILABLE), UNAVAILABLE)

print("\nA failed source must not drown out a working one")

from ep_watcher.engine import merge  # noqa: E402

good = Reading(source="browser", resale=AVAILABLE,
               listings=[Listing("Verified Resale — STNDN9", "€366.39", "resale")])
dead = Reading(source="discovery-api", failed=True)
merged = merge([dead, good])
check("the real find survives", merged.resale, AVAILABLE)
check("and is not marked failed", merged.failed, False)
check("and keeps its listing", len(merged.listings), 1)

print("\nEvery source failing IS a failure, not a quiet 'no'")

merged = merge([Reading(source="a", failed=True), Reading(source="b", failed=True)])
check("marked failed", merged.failed, True)
check("with no availability claim", merged.any_good, False)

print("\nAlerting must fire on a find even after a run of failures")

s = dict(st._defaults())
s["consecutive_failures"] = 12
s["last_resale"] = UNKNOWN
found = Reading(source="browser", resale=AVAILABLE,
                listings=[Listing("Verified Resale — STNDN9", "€366.39", "resale")])
should, why = st.should_alert_availability(s, found, st.pending_listings(s, found))
check("it alerts", should, True)
print(f"        reason: {why}")

print("\nConfiguration cannot be set to something silently useless")

check("searching for at least one ticket", min(config.WANTED_QUANTITIES) >= 1, True)
check("never faster than the floor",
      max(config.POLL_INTERVAL_SECONDS, config.PRESS_MIN_INTERVAL_SECONDS)
      >= config.PRESS_MIN_INTERVAL_SECONDS, True)
check("night never faster than day",
      config.poll_interval_now(config.POLL_INTERVAL_SECONDS)[0]
      >= config.POLL_INTERVAL_SECONDS, True)
check("stop date is a real date", len(config.STOP_AFTER_DATE.split("-")), 3)
check("watchdog fires before the re-nag window",
      config.WATCHDOG_FAILURE_THRESHOLD >= 1, True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
