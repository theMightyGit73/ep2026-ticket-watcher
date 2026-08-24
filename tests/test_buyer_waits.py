"""The buyer must wait for the resale panel before deciding a listing is gone.

Fourteen listings have been recorded. Six securing attempts have run. None has
ever held a ticket, and by 2026-08-19 the reason had narrowed to one line of
sequencing.

Three of the six died on the Playwright asyncio fault and were fixed by giving
the buyer its own thread. The other three — 17:58, 19:05 and 19:12 on
2026-08-19 — got as far as opening the buying browser, pressing search, and
reporting "the listing was gone from the page by the time the buying browser
reached it".

They were almost certainly all wrong. The buyer pressed search and looked for
the listing row five seconds later. The watcher's own source establishes, at
length and from measurement, that pressing search does not produce listings:
the search resolves, and only then does a separate /api/quickpicks call come
back and paint the panel. The watcher gives that up to 25 seconds plus render
and settle time, and reading the page early once recorded a quarter of its
polls as resale-blind. Five seconds is not a race being lost, it is looking
before anything has been drawn.

The verdict was the worse half of the bug. "Gone" reads as losing a race,
which invites making the watcher faster — and no amount of speed fixes looking
too early.

Run with:  .venv/bin/python tests/test_buyer_waits.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer, config  # noqa: E402
from ep_watcher.model import Listing  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


LISTING = Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                  listing_id="l27t4h2d", section="STNDN1")
EVENT = config.EVENTS[0]


class FakeButton:
    def __init__(self, label="Find Tickets"):
        self.label, self.clicked = label, False

    def is_visible(self, timeout=None): return True
    def inner_text(self, timeout=None): return self.label
    def click(self, timeout=None): self.clicked = True


class _First:
    def __init__(self, obj): self.first = obj


class FakePage:
    def __init__(self, rows_visible, body=""):
        self.rows_visible = rows_visible
        self.body = body
        self.url = "https://www.ticketmaster.ie/checkout/x"

    def goto(self, url, **kw): pass
    def get_by_role(self, role, name=None, exact=False): return _First(FakeButton())
    def inner_text(self, _sel): return self.body
    def bring_to_front(self): pass

    def get_by_text(self, text, exact=False):
        class Row:
            def __init__(self, visible): self._v = visible
            def is_visible(self, timeout=None): return self._v
            def click(self, timeout=None): pass
        return _First(Row(self.rows_visible))


class FakeSession:
    """Records the order in which secure() asks it to do things."""

    def __init__(self, rows_visible=False, panel_readable=True, picks=None):
        self.calls = []
        self.page = FakePage(rows_visible)
        self._panel_readable = panel_readable
        self._picks = picks

    def set_quantity(self, qty, result):
        self.calls.append("set_quantity")

    def await_listings(self, result, budget_s):
        self.calls.append("await_listings")
        return self._panel_readable

    def listings_now(self, event, qty):
        self.calls.append("listings_now")
        if self._picks is None:
            return None
        return {"status": 200, "data": {"picks": self._picks}}


def run(session):
    return buyer.secure(session, EVENT, LISTING, buyer.HoldResult())


print("\nThe sequencing, which is the whole bug")
session = FakeSession(rows_visible=False, picks=[])
run(session)
check_true("the panel is waited for", "await_listings" in session.calls)
check("and it happens BEFORE the row is hunted, not after",
      session.calls.index("await_listings") < session.calls.index("listings_now"),
      True)
check("and after the search, not before it",
      session.calls.index("set_quantity") < session.calls.index("await_listings"),
      True)


print("\n'Gone' may only be claimed when the endpoint says so")
# The endpoint the panel is a drawing of is the arbiter. Asking it turns a
# guess into a fact, and the two answers need opposite responses from David.
session = FakeSession(rows_visible=False, picks=[])
hold = run(session)
check("nothing was held", hold.secured, False)
# Reworded 2026-08-24. This used to require the words "genuinely sold" and
# "race was lost", and the check was right about the SHAPE — an empty endpoint
# must read differently from one that still lists the ticket — while being
# wrong about the certainty. An empty feed is also what a withdrawal looks
# like, and what 49 consecutive refusals look like, so "sold" was a guess
# stated as a finding. The distinction being defended is unchanged; only the
# confidence is.
check_true("an empty endpoint reads as no longer offered",
           "no longer being offered" in hold.reason)
check_true("names a sale as the likely cause, not the certain one",
           "Most likely sold" in hold.reason)
check_true("and admits the alternatives cannot be told apart",
           "Not distinguishable" in hold.reason)

print("\nAnd when the endpoint still has it, that is NOT a lost race")
# This is the case that was misreported three times. The listing is there, the
# row was not found: a rendering or selector problem, and one David can still
# act on by hand because the ticket is genuinely reachable.
session = FakeSession(rows_visible=False, picks=[{"id": "l27t4h2d", "section": "STNDN1"}])
hold = run(session)
check("still nothing held", hold.secured, False)
check("but it is not blamed on the listing being gone",
      "genuinely sold" in hold.reason, False)
check_true("it names the real problem", "not a lost race" in hold.reason)
check_true("says how many are still on offer", "1 listing(s)" in hold.reason)
check_true("and tells him it is still reachable by hand",
           "by hand" in hold.reason)

print("\nAn unanswerable endpoint is admitted, not guessed at")
session = FakeSession(rows_visible=False, picks=None)
hold = run(session)
check_true("the reason says the cause is unknown", "unknown" in hold.reason)
check("and does not assert it sold", "genuinely sold" in hold.reason, False)

print("\nThe budget has to be big enough to contain the waiting")
# Searching alone can take 30s on a slow link, and the panel is a separate
# call after that. The old 45s could not have fitted both even if it had tried.
check_true(f"the secure budget ({config.SECURE_TIMEOUT_SECONDS}s) allows a slow "
           f"search and a panel after it", config.SECURE_TIMEOUT_SECONDS >= 90)

print("\nWaiting must never turn into paying")
# The guard that matters most is unaffected by any of this.
for label in ("Place Order", "Pay now", "Continue to payment", "Cancel Order"):
    check_true(f"still refuses {label!r}", buyer.is_forbidden(label))

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
