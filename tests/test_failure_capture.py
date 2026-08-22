"""Every failed attempt has to leave enough behind to fix the next one.

David's instruction of 2026-08-21: "capture how we failed so the next time we
will succeed."

By that afternoon the watcher had made fourteen securing attempts and held
nothing, and the record of them was fourteen sentences of prose. That was
enough to tell the story wrong for two days — every loss was filed as a race
lost at the click, on the strength of a check the code could not actually
perform — and it was not enough to explain the one failure nobody could
account for.

That failure is the shape of the problem. At 12:06 and again at 12:07 the
buying browser reported "no quantity stepper found" AND "the search button
never became clickable", before and after a reload, forty seconds apart. Two
controls that are always on a real event page, both missing, twice. That is
not a stale page — it is a different page, almost certainly a block or a
challenge screen. One URL would have settled it. Nothing wrote one down.

So now a failed attempt writes a record: where the browser actually was, what
the page said, which of the controls it should have had were present, and
whether any of it looks like an interstitial rather than the event.

What must stay true: this never runs on the critical path, and it never turns
a recorded failure into an unrecorded one. The find recorder dropped its own
screenshot in August because it sat between a live listing and the click that
might win it. This fires only once the ticket is already lost.

Run with:  .venv/bin/python tests/test_failure_capture.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


EVENT = config.EVENTS[0]

REAL_PAGE = ("Electric Picnic 2026 Find Tickets quantity Verified Resale "
             "Tickets will appear below")
BLOCK_PAGE = ("Access Denied  Please verify you are a human before continuing. "
              "Request blocked.")


class FakeLocator:
    def __init__(self, visible): self.visible = visible; self.first = self
    def is_visible(self, timeout=None): return self.visible


class FakePage:
    def __init__(self, url, text, title="", stepper=True, explode=False):
        self._url, self.text, self._title = url, text, title
        self.stepper, self.explode = stepper, explode
        self.shots = []

    @property
    def url(self):
        if self.explode:
            raise RuntimeError("page is gone")
        return self._url

    def title(self):
        if self.explode:
            raise RuntimeError("page is gone")
        return self._title

    def inner_text(self, _sel):
        if self.explode:
            raise RuntimeError("page is gone")
        return self.text

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self.stepper and role == "spinbutton")

    def screenshot(self, path=None, full_page=False, timeout=None):
        if self.explode:
            raise RuntimeError("cannot photograph a dead page")
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.shots.append(path)


def read_records():
    return sorted(config.DIAG_DIR.glob("hold-*.json"))


def latest():
    """The most recently written record, by mtime rather than by name.

    Not by name: the collision guard in capture_failure appends a counter, so
    a second record written inside the same millisecond sorts BEFORE its
    predecessor rather than after it.
    """
    newest = max(read_records(), key=lambda p: p.stat().st_mtime_ns)
    return json.loads(newest.read_text())


print("\nA failed attempt writes down where the browser actually was")
result = buyer.HoldResult()
result.reason = "could not press search in the buying browser"
result.note("the search button never became clickable")
page = FakePage("https://www.ticketmaster.ie/electric-picnic-2026",
                REAL_PAGE, title="Electric Picnic 2026")
path = buyer.capture_failure(page, result, EVENT, attempt=1)
check_true("a record is written", path and Path(path).exists())
rec = latest()
check("the URL is kept", rec["url"], "https://www.ticketmaster.ie/electric-picnic-2026")
check("and the page title", rec["title"], "Electric Picnic 2026")
check("and which page it was", rec["event"], EVENT.slug)
check("and which try", rec["attempt"], 1)
check_true("and the reason", "could not press search" in rec["reason"])
check_true("and the notes, so the sequence survives", rec["notes"])


print("\nIt records which controls the page actually had")
# The line that would have answered 12:06 in one glance. "No stepper AND no
# search button" is a completely different diagnosis from either alone, and
# the log could only ever say them one at a time.
check("a real page has its search button", rec["page"]["has_find_tickets"], True)
check("and its stepper", rec["page"]["has_quantity_stepper"], True)
check("and nothing looks like a challenge screen",
      rec["page"]["looks_like_interstitial"], [])


print("\nAnd it says plainly when the browser is not on the event page at all")
result = buyer.HoldResult()
result.reason = "could not press search in the buying browser even after reloading"
page = FakePage("https://www.ticketmaster.ie/blocked", BLOCK_PAGE,
                title="Access Denied", stepper=False)
buyer.capture_failure(page, result, EVENT, attempt=2)
rec = latest()
check("no search button", rec["page"]["has_find_tickets"], False)
check("no stepper either", rec["page"]["has_quantity_stepper"], False)
check_true("and it is named as a challenge screen",
           rec["page"]["looks_like_interstitial"])
check_true("naming what it matched",
           "access denied" in rec["page"]["looks_like_interstitial"])
check("the attempt number distinguishes the retry", rec["attempt"], 2)


print("\nA challenge page with no readable body is still recognised")
# The real one, verbatim, from 2026-08-22 at 11:23 UTC. It is the reason this
# check reads the title and not only the body: Ticketmaster served the correct
# event URL, zero characters of text, and every control missing. A body-only
# matcher — which is what this was when it shipped — reported nothing unusual
# about the most important page it had ever been shown.
page = FakePage(
    "https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping"
    "-co-laois-28-08-2026/event/18006314BD813D3E",
    "", title="Your Browsing Activity Has Been Paused", stepper=False)
buyer.capture_failure(page, buyer.HoldResult(), EVENT)
rec = latest()
check("the body really is empty", rec["page"]["text_chars"], 0)
check("the URL still looks like the event page",
      "18006314BD813D3E" in rec["url"], True)
check_true("but it is named as a challenge screen",
           rec["page"]["looks_like_interstitial"])
check_true("by the exact wording Ticketmaster used",
           "your browsing activity has been paused"
           in rec["page"]["looks_like_interstitial"])


print("\nThe page text is kept, and bounded")
# Enough to read the headline of a block page; not so much that a fortnight of
# failures fills the disk with Ticketmaster markup.
big = FakePage("https://www.ticketmaster.ie/x", "y" * 50_000)
buyer.capture_failure(big, buyer.HoldResult(), EVENT)
rec = latest()
check("the excerpt is capped", len(rec["text_excerpt"]), 4000)
check_true("but the true size is still recorded",
           rec["page"]["text_chars"] == 50_000)


print("\nA picture, because that is what settles 'what page IS this'")
was = config.HOLD_SCREENSHOTS
try:
    config.HOLD_SCREENSHOTS = True
    page = FakePage("https://www.ticketmaster.ie/z", BLOCK_PAGE)
    path = buyer.capture_failure(page, buyer.HoldResult(), EVENT)
    check_true("one is taken", page.shots)
    check_true("beside the record", Path(path).with_suffix(".png").exists())

    config.HOLD_SCREENSHOTS = False
    page = FakePage("https://www.ticketmaster.ie/z", BLOCK_PAGE)
    path = buyer.capture_failure(page, buyer.HoldResult(), EVENT)
    check("and the switch really switches it off", page.shots, [])
    check("while the record is still written",
          Path(path).exists(), True)
finally:
    config.HOLD_SCREENSHOTS = was


print("\nA diagnostic must never break the thing it is diagnosing")
# This runs inside _secure_once's finally, on the path where a ticket has just
# been lost. Anything it raises would replace a recorded failure with an
# exception out of the middle of the buyer.
before = len(read_records())
dead = FakePage("gone", "", explode=True)
raised = False
try:
    buyer.capture_failure(dead, buyer.HoldResult(), EVENT)
except Exception:
    raised = True
check("a page that cannot be read does not raise", raised, False)
check_true("and a record is still written from what was known",
           len(read_records()) > before)
rec = latest()
check("with the unreadable fields left empty rather than guessed",
      (rec["url"], rec["title"]), ("", ""))

# A screenshot that fails must not lose the record either.
was = config.HOLD_SCREENSHOTS
try:
    config.HOLD_SCREENSHOTS = True
    before = len(read_records())
    shy = FakePage("https://www.ticketmaster.ie/q", REAL_PAGE)
    shy.screenshot = lambda **kw: (_ for _ in ()).throw(RuntimeError("no"))
    buyer.capture_failure(shy, buyer.HoldResult(), EVENT)
    check_true("a failed photograph still leaves the record",
               len(read_records()) > before)
finally:
    config.HOLD_SCREENSHOTS = was


print("\nThe forensics from the attempt itself are carried into the record")
# So one file answers the whole question, rather than needing the event log
# open beside it.
result = buyer.HoldResult()
result.still_listed_after = True
result.ever_listed_after = True
result.ids_after = ["l1jwc8k6"]
result.listing_id = "l1jwc8k6"
result.attempts = 3
result.timings["panel"] = 17.7
buyer.capture_failure(FakePage("https://secure.ticketmaster.ie/error/q404",
                               "sold or removed"), result, EVENT, attempt=3)
rec = latest()
check("whether the feed still listed it", rec["still_listed_after"], True)
check("whether it EVER did, across the retries", rec["ever_listed_after"], True)
check("the ids the feed returned", rec["ids_after"], ["l1jwc8k6"])
check("the id we were chasing", rec["listing_id"], "l1jwc8k6")
check("and where the seconds went", rec["timings"]["panel"], 17.7)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
