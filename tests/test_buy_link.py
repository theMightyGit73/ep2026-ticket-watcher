"""The alert must land David on the listing, not on the event.

On 2026-08-19 he reported the actual failure mode: the alert arrives, and by
the time he has found the page, set the quantity to one and pressed search,
the listing is gone — or is still shown but refuses on the next screen. Every
listing recorded so far has been visible on exactly one poll, so the seconds
between opening the alert and seeing the ticket are the whole product.

These checks pin the parts of that link that are load-bearing, and pin the
thing that must NOT change: the listing id stays out of describe(), because
that string is what the new-listing diff compares.

Run with:  .venv/bin/python tests/test_buy_link.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, notify  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


EVENT = (
    "https://www.ticketmaster.ie"
    "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
    "/event/18006314BD813D3E"
)


def resale(listing_id="ly7vs38jkx", section="STNDN1", price="€366.39"):
    """The listing found at 19:04 on 2026-08-18, as the API returned it."""
    return Listing(
        name=f"Verified Resale — Section {section} (WEEKEND CAMPING)",
        price=price,
        kind="resale",
        listing_id=listing_id,
        section=section,
    )


print("\nThe link itself")
# The quantity is the one parameter we are sure matters: the page defaults to
# 2, resale results are filtered by quantity, and a single ticket is invisible
# to a search for two.
check("carries the quantity", "quantity=1" in notify.buy_url(EVENT, 1), True)
check(
    "defaults to the wanted quantity, not a literal",
    f"quantity={config.WANTED_QUANTITY}" in notify.buy_url(EVENT),
    True,
)
check(
    "appends to an existing query string",
    notify.buy_url(EVENT + "?foo=bar", 1).endswith("?foo=bar&quantity=1"),
    True,
)
check(
    "does not produce two question marks",
    notify.buy_url(EVENT + "?foo=bar", 1).count("?"),
    1,
)
check(
    "carries the listing id when known",
    notify.buy_url(EVENT, 1, resale()).endswith("#resale-ly7vs38jkx"),
    True,
)
# The count-only shape has been seen in the wild when the API reported a total
# but returned no detail. It must still produce a usable link.
bare = Listing(name="Verified Resale (count only)", kind="resale")
check("survives a listing with no id", "#resale-" in notify.buy_url(EVENT, 1, bare), False)
check("still links at all with no id", "quantity=1" in notify.buy_url(EVENT, 1, bare), True)
check("an empty event url stays empty", notify.buy_url("", 1), "")

print("\nListing identity must not shift (the re-alert trap)")
# describe() drives the new-listing diff. If the id leaked into it and
# Ticketmaster regenerates ids per poll, the same ticket would look new on
# every check and re-alert on a four-minute clock.
check("id stays out of describe()", "ly7vs38jkx" in resale().describe(), False)
check("section stays in describe()", "STNDN1" in resale().describe(), True)
check("price stays in describe()", "€366.39" in resale().describe(), True)
check(
    "two sightings describe identically even if the id changes",
    resale(listing_id="aaa").describe() == resale(listing_id="bbb").describe(),
    True,
)

print("\nChoosing which listing to point at")
primary = Listing(name="General Admission", kind="primary")
check("resale beats primary", notify._best_listing([primary, resale()]).kind, "resale")
check(
    "a linkable listing beats a bare one",
    notify._best_listing([bare, resale()]).listing_id,
    "ly7vs38jkx",
)
check("nothing to choose from is None", notify._best_listing([]), None)

print("\nThe lock-screen headline")
check("section and price", notify._headline(resale()), "Section STNDN1 · €366.39")
# The find of 2026-08-18 10:35 had a section and no price, because the price
# key was not yet known. That must degrade, not crash.
check("survives no price", notify._headline(resale(price=None)), "Section STNDN1")
check("says something for nothing", bool(notify._headline(None)), True)

print("\nThe assembled alert")
sent, pushed = {}, {}
notify._send_email = lambda subject, body: sent.update(subject=subject, body=body) or True
notify._push = lambda label, **kw: pushed.update(kw) or True

reading = Reading(source="browser")
reading.event_name = "Electric Picnic 2026 - Weekend Camping"
reading.event_url = EVENT
reading.primary = UNAVAILABLE
reading.resale = AVAILABLE
reading.listings.append(resale())
notify.available(reading, "resale appeared", [resale().describe()])

check("email carries the link", "quantity=1" in sent["body"], True)
# The link is near the top, not buried under the four-step recipe it sat
# under before 2026-08-19.
check(
    "link is in the first third of the email",
    sent["body"].index("quantity=1") < len(sent["body"]) / 3,
    True,
)
check("subject carries the section", "STNDN1" in sent["subject"], True)
check("subject carries the price", "€366.39" in sent["subject"], True)
check("push title carries the section", "STNDN1" in pushed["title"], True)
check("push click-through carries the quantity", "quantity=1" in pushed["click"], True)
check("push is urgent", pushed["priority"], "urgent")
# The plain event page stays in the body as a fallback, because the link
# above it is a hypothesis about Ticketmaster's routing, not an observation.
check("plain event url kept as a fallback", EVENT in sent["body"], True)

print("\nThe alert must name the right page")
sent.clear()
other = Reading(source="browser")
other.event_name = "Electric Picnic 2026 - Weekend Camping Instalment Plan"
other.event_url = "https://www.ticketmaster.ie/instalment/event/18006314CFB4A99E"
other.resale = AVAILABLE
other.listings.append(resale())
notify.available(other, "resale appeared", [])

check("names the instalment plan", "Instalment Plan" in sent["subject"], True)
check("spells out which page", "INSTALMENT PLAN" in sent["body"], True)
check("links the instalment page", "18006314CFB4A99E" in sent["body"], True)
check("does not leak the other page", "18006314BD813D3E" in sent["body"], False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
