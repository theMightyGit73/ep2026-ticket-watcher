"""Read resale listings off the real page text, in both states.

Both fixtures below are verbatim captures from ticketmaster.ie, not invented:
the empty one from 2026-08-13 23:12 local, minutes after a listing sold, and
the populated one from earlier the same day.

The two differ less than you would hope. The heading "Verified Resale
Tickets" and the caption "Resale Tickets will appear below when they are
available." are present in BOTH — that caption is static furniture, not an
empty-state marker, and reading it as one inverts the result. So is
"eTickets FREE". The only real difference is a block naming a section, the
words "Verified Resale Ticket" (singular), and a price.

Getting this wrong is expensive in both directions: a false negative misses
the ticket, and a false positive sends you running at a page with nothing on
it, which is how you learn to ignore the alerts.

Run with:  .venv/bin/python tests/test_resale_parsing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher.model import AVAILABLE, UNAVAILABLE, Reading  # noqa: E402
from ep_watcher.sources import browser  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


# Captured after the STNDN2 listing sold — the panel is up, and empty.
EMPTY = """There aren't enough tickets to complete your request
Please update the quantity or ticket type to see available tickets.

Search Again
Other Options
Verified Resale Tickets
Resale Tickets will appear below when they are available.
eTickets FREE


Other Dates

Other Events at Venue"""

# Captured while a listing was genuinely on sale.
WITH_LISTING = """There aren't enough tickets to complete your request
Please update the quantity or ticket type to see available tickets.
Search Again
Other Options
Verified Resale Tickets
Resale Tickets will appear below when they are available.

eTickets FREE

Section STNDN1
Verified Resale Ticket
WEEKEND CAMPING
€366.39 each
Other Dates
Other Events at Venue"""

TWO_LISTINGS = WITH_LISTING.replace(
    "Other Dates",
    "Section STNDN2\nVerified Resale Ticket\nWEEKEND CAMPING\n€410.00 each\nOther Dates",
)


class FakePage:
    """Just enough BrowserSession to exercise the parser offline."""

    def __init__(self, text):
        self.text = text

    def visible_text(self):
        return self.text


def parse(text):
    session = FakePage(text)
    reading = Reading(source="test")
    browser.BrowserSession._parse_resale(session, browser._normalise(text), reading)
    return reading


print("\nAn empty panel must never look like a ticket")

r = parse(EMPTY)
check("reports nothing available", r.resale, UNAVAILABLE)
check("and finds no listings", len(r.listings), 0)
check_note = any("no listings" in n for n in r.notes)
check("says the panel was rendered but empty", check_note, True)

print("\nA real listing must be found")

r = parse(WITH_LISTING)
check("reports available", r.resale, AVAILABLE)
check("finds exactly one", len(r.listings), 1)
listing = r.listings[0]
check("with the section", "STNDN1" in listing.name, True)
check("with the description", "WEEKEND CAMPING" in listing.name, True)
check("with the price", listing.price, "€366.39")
check("marked as resale", listing.kind, "resale")

print("\nSeveral listings at once")

r = parse(TWO_LISTINGS)
check("finds both", len(r.listings), 2)
check("keeps them distinct",
      len({l.describe() for l in r.listings}), 2)

print("\nThe static caption must not be mistaken for an empty state")
# It is present in every fixture above, including the ones with listings.
for name, text in (("empty", EMPTY), ("with listing", WITH_LISTING)):
    caption = "resale tickets will appear below when they are available" in text.lower()
    check(f"caption present in the {name} page", caption, True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
