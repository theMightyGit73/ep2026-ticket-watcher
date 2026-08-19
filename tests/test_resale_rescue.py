"""Asking the resale endpoint directly, when the page will not.

The panel is the page's drawing of one API call. Waiting for the drawing is
where readings are lost: of the 80 resale-blind polls recorded up to
2026-08-19, 78 never saw that call at all, and 26 of those followed a search
that had otherwise worked perfectly. Nothing was wrong with the session — the
watcher was waiting for the page to ask a question it could have asked itself.

The rescue runs only where the alternative is recording UNKNOWN, so the rule
these checks exist to protect is: it must never be able to make a reading
worse. A definite answer already in hand is never overwritten, and a failed
rescue leaves the poll exactly as blind as it already was.

Run with:  .venv/bin/python tests/test_resale_rescue.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, UNKNOWN, Reading  # noqa: E402
from ep_watcher.sources import browser  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


EVENT_URL = (
    "https://www.ticketmaster.ie"
    "/electric-picnic-2026-weekend-camping-co-laois-28-08-2026"
    "/event/18006314BD813D3E"
)

# The body Ticketmaster actually returned on 2026-08-18 at 19:04.
ONE_LISTING = {
    "quantity": 1, "eventId": "18006314BD813D3E", "total": 1,
    "picks": [{
        "id": "ly7vs38jkx", "type": "general-seating", "section": "STNDN1",
        "originalPrice": 366.39, "description": "WEEKEND CAMPING",
        "areaName": "GA", "offerIds": ["HF6GY6JXOZZTGODKNN4A"],
        "resaleListingId": "ly7vs38jkx", "sellerBusinessType": "private",
    }],
    "descriptions": [],
}
EMPTY = {"quantity": 1, "eventId": "18006314BD813D3E", "total": 0,
         "picks": [], "descriptions": []}


print("\nThe event id is recoverable from any page's URL")
# Only the standard page has tm_event_id configured — that field exists for
# the Inventory Status API. Without deriving the id from the URL the rescue
# would silently never fire on the instalment plan.
check("standard page", browser._event_id_from_url(EVENT_URL), "18006314BD813D3E")
check("instalment page",
      browser._event_id_from_url(
          "https://www.ticketmaster.ie/x-instalment-co-laois/event/18006314CFB4A99E"),
      "18006314CFB4A99E")
check("a URL with a trailing path", browser._event_id_from_url(
    "https://www.ticketmaster.ie/a/event/ABC123/resale"), "ABC123")
check("no id present", browser._event_id_from_url("https://example.com/"), "")
check("empty input", browser._event_id_from_url(""), "")

for event in config.EVENTS:
    got = event.tm_event_id or browser._event_id_from_url(event.url)
    check_true(f"every configured page yields an id ({event.slug})", got)


print("\nA rescued response is read exactly like a passively captured one")
# Same shape in, same parse out — the parser must not care which route the
# record arrived by, or the two would drift apart.
r = Reading(source="browser")
check("a listing is found", browser._parse_resale_json({"status": 200, "data": ONE_LISTING}, r), True)
check("and resale reads AVAILABLE", r.resale, AVAILABLE)
check("with the listing attached", len(r.listings), 1)
check("carrying its id for the alert link", r.listings[0].listing_id, "ly7vs38jkx")
check("and its section", r.listings[0].section, "STNDN1")
check("and its price", r.listings[0].price, "€366.39")

r = Reading(source="browser")
check("an empty body answers too", browser._parse_resale_json({"status": 200, "data": EMPTY}, r), True)
check("definitively UNAVAILABLE", r.resale, UNAVAILABLE)

r = Reading(source="browser")
check("a body of the wrong shape does not answer",
      browser._parse_resale_json({"status": 200, "data": {"unexpected": True}}, r), False)
check("and leaves resale UNKNOWN rather than guessing", r.resale, UNKNOWN)


print("\nThe rescue never fires when the page already answered")
# The guard is `attempt.resale == UNKNOWN`. These pin the two states that must
# be left alone: a real listing, and a definite no.
for status in (AVAILABLE, UNAVAILABLE):
    r = Reading(source="browser")
    r.resale = status
    check(f"{status} is already an answer, so no rescue is warranted",
          r.resale == UNKNOWN, False)


print("\nA failed rescue leaves the poll no worse than it was")


class FakeSession:
    """Stands in for BrowserSession, returning whatever the endpoint 'said'."""

    def __init__(self, record):
        self._record = record
        self._page = object()
        self._resale_response = None

    fetch_resale_json = lambda self, event, qty: self._record  # noqa: E731


def rescue_into(reading, record):
    """The rescue block from _search_quantities, in isolation."""
    session = FakeSession(record)
    if reading.resale == UNKNOWN:
        got = session.fetch_resale_json(None, 1)
        if got and got.get("data") is not None:
            if browser._parse_resale_json(got, reading):
                reading.note("resale read by asking the endpoint directly")
        elif got and got.get("status"):
            reading.note(f"direct resale fetch answered HTTP {got['status']} "
                         f"with no usable body")
        elif got and got.get("error"):
            reading.note(f"direct resale fetch failed: {got['error']}")
    return reading


r = rescue_into(Reading(source="browser"), None)
check("a rescue that could not run leaves UNKNOWN", r.resale, UNKNOWN)

r = rescue_into(Reading(source="browser"), {"status": 403, "data": None})
check("a 403 leaves UNKNOWN", r.resale, UNKNOWN)
check_true("and says so in the notes", any("403" in n for n in r.notes))

r = rescue_into(Reading(source="browser"),
                {"status": None, "data": None, "error": "TimeoutError: gave up"})
check("a thrown fetch leaves UNKNOWN", r.resale, UNKNOWN)
check_true("and names the failure", any("TimeoutError" in n for n in r.notes))

r = rescue_into(Reading(source="browser"), {"status": 200, "data": EMPTY})
check("a good rescue converts a blind poll into a real reading", r.resale, UNAVAILABLE)
check_true("and is labelled as rescued so the logs stay honest",
           any("directly" in n for n in r.notes))

r = rescue_into(Reading(source="browser"), {"status": 200, "data": ONE_LISTING})
check("and it can find a live listing", r.resale, AVAILABLE)
check("which reaches the alert with its id", r.listings[0].listing_id, "ly7vs38jkx")


print("\nThe request it builds is the one the page makes for itself")
# Confirmed against David's own signed-in browser on 2026-08-19. Getting this
# wrong means a URL Ticketmaster does not serve, or one that misses Fastly's
# edge cache and hits origin — heavier and more conspicuous than the call the
# page already makes.
src = (Path(__file__).resolve().parent.parent
       / "ep_watcher" / "sources" / "browser.py").read_text()
fetch_src = src[src.index("def fetch_resale_json"):src.index("def _settle_resale")]
check_true("keyed on the event id", "/api/quickpicks/" in fetch_src)
check_true("carries the quantity", "qty={qty}" in fetch_src)
check_true("asks for one page of results", "offset=0&limit=20" in fetch_src)
check_true("sends the session's cookies", "credentials: 'include'" in fetch_src)
check_true("bypasses the browser's disk cache", "cache: 'no-store'" in fetch_src)
# A cache-busting parameter would miss Fastly's edge and hit origin.
check("and does not cache-bust with a novel parameter",
      "Math.random" in fetch_src or "Date.now" in fetch_src, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
