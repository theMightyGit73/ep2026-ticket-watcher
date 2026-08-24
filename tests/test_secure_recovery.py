"""What a securing attempt does when the page does not cooperate.

Every one of these is a real failure taken from the log of the fourteen
attempts made between 2026-08-19 and 2026-08-21, none of which secured
anything. They are grouped here because they share a shape: the attempt met
something it could have recovered from or reported, and instead returned
silently with the ticket lost.

  * 2026-08-21 05:57. The search button never became clickable. The attempt
    spent ten seconds setting a quantity, fifteen more waiting for a button,
    and gave up — on a real weekend listing. "Waiting for the search button
    to be visible" is the most common browser failure this project has,
    thirteen occurrences, and a stale parked page is the likeliest cause. A
    reload is the obvious answer and there was not one.

  * 2026-08-20 20:02, and twice before it. The panel drew no row, the code
    asked the feed whether the ticket had really gone, and kept the answer in
    a local variable — so `still_listed_after` stayed None, the forensics
    recorded null for a question that had been answered, and secure() could
    never decide to go back for a ticket that had not sold.

  * Every attempt so far. `landed_url` was captured on one of five failure
    paths, and both attempts recorded since it was added failed before
    reaching that one. The field is the only place a direct link to a single
    listing has ever been seen, and it has been "" for its whole life.

Run with:  .venv/bin/python tests/test_secure_recovery.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402

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


EVENT = config.EVENTS[0]
LISTING = Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                  listing_id="ly7vs38jkx", section="STNDN1")


# ── The fakes ────────────────────────────────────────────────────────────────
#
# Deliberately minimal. The properties under test are about control flow —
# does it reload, does it record — and a fuller browser double would only add
# ways for the test to be wrong.

class FakeLocator:
    """A control that fails a set number of times before it works."""

    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.first = self

    def click(self, timeout=None):
        if self.kind == "search":
            self.page.search_clicks += 1
            if self.page.search_fails > 0:
                self.page.search_fails -= 1
                raise PlaywrightTimeout("Locator.click: Timeout 15000ms exceeded.")
            self.page.searched = True
            return
        self.page.row_clicks += 1

    def is_visible(self, timeout=None):
        return self.kind == "row" and self.page.rows_visible


class FakePage:
    def __init__(self, url, search_fails=0, rows_visible=False,
                 title="", blocked_until_try=0):
        self.url = url
        self._title = title
        # Serves the block screen for the first N *attempts*, then relents —
        # which is what a real challenge does, and the whole reason waiting it
        # out is worth anything.
        self.blocked_until_try = blocked_until_try
        self.tries_seen = 0
        self.search_fails = search_fails
        self.search_clicks = 0
        self.searched = False
        self.rows_visible = rows_visible
        self.row_clicks = 0
        self.reloads = 0
        self.body = ""

    def goto(self, url, wait_until=None, timeout=None):
        # `timeout` is part of Playwright's real signature. Without it here the
        # double raised TypeError the moment production code passed one, and a
        # TypeError from a test double reads as a failure in the code under
        # test rather than as a gap in the fake.
        self.reloads += 1
        self.url = url

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, "search")

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, "row")

    def inner_text(self, _sel="body"):
        return self.body

    def title(self):
        return self._title


class FakeSession:
    """Just enough BuySession for _secure_once, plus a scripted feed."""

    def __init__(self, page, feed=None):
        self._page = page
        self.quantity_calls = 0
        self.feed = feed
        self.feed_calls = 0

    @property
    def page(self):
        return self._page

    def set_quantity(self, qty, result):
        self.quantity_calls += 1

    def await_listings(self, result, budget_s):
        return True

    def listings_now(self, event, qty):
        self.feed_calls += 1
        return self.feed


def payload(ids):
    return {"status": 200,
            "data": {"picks": [{"resaleListingId": i} for i in ids],
                     "total": len(ids)}}


# session_evidence() reads a real cookie database. Nothing here is about
# sign-in state, and letting it touch the filesystem would make these tests
# depend on whether a browser profile happens to exist.
buyer.session_evidence = lambda *a, **k: {"signed_in": None, "reason": "test"}

# Every case below is about the search path — the button that will not appear,
# the panel that draws no row, the probe on the dead end. Since 2026-08-24 that
# path is the FALLBACK: an attempt now goes straight to the offer URL first,
# because the page's own click built that link with qty=0 and lost every ticket
# to it. The direct path is covered in test_offer_trace.py; switching it off
# here keeps these tests exercising the thing they were written for, rather
# than skipping it and quietly testing nothing.
config.DIRECT_OFFER = False


print("\nA block screen is recognised as a block, not as a missing button")
# 2026-08-22, three times: 11:23, 12:23 and 14:36. Ticketmaster served the
# buying browser "Your Browsing Activity Has Been Paused" — correct event URL,
# no readable text, no controls — and every one was reported as "could not
# press search", which reads as a selector problem and is not. A challenge and
# a slow page produce the identical Playwright timeout; only the page can tell
# them apart.
page = FakePage(EVENT.url, search_fails=9,
                title="Your Browsing Activity Has Been Paused")
result = buyer._secure_once(FakeSession(page, feed=payload([])), EVENT, LISTING)
check_true("it is flagged as a block", result.challenged)
check_true("and says so in words David can act on",
           "block screen" in (result.reason or ""))
check_true("naming what it matched",
           "browsing activity" in (result.reason or ""))
check("and does not blame the selector",
      "could not press search" in (result.reason or ""), False)

# An ordinary timeout on a real page is still an ordinary timeout.
page = FakePage(EVENT.url, search_fails=9, title="Electric Picnic 2026")
result = buyer._secure_once(FakeSession(page, feed=payload([])), EVENT, LISTING)
check("a slow page is not called a block", result.challenged, False)
check_true("and reports the timeout plainly",
           "could not press search" in (result.reason or ""))


print("\nA block is waited out — it used to end the attempt on the first try")
# The block fails before the resale panel, so still_listed_after is never set,
# and secure() read that as "nothing to come back for": one try, fifty
# seconds, done. All three blocked attempts of 2026-08-22 show tries=1 while
# the retry budget sat at six.
pauses = []
real_sleep = time.sleep
time.sleep = lambda s: pauses.append(s)
was = (config.SECURE_CHALLENGE_PAUSE_SECONDS, config.SECURE_RETRY_PAUSE_SECONDS)
try:
    config.SECURE_CHALLENGE_PAUSE_SECONDS = 0.0
    config.SECURE_RETRY_PAUSE_SECONDS = 0.0

    blocked = FakePage(EVENT.url, search_fails=99,
                       title="Your Browsing Activity Has Been Paused")
    out = buyer.secure(FakeSession(blocked, feed=payload([])), EVENT, LISTING)
    check("it goes back for it", out.attempts,
          1 + config.SECURE_CHALLENGE_RETRIES)
    check_true("waiting in between", len(pauses) >= config.SECURE_CHALLENGE_RETRIES)
    check_true("and stops rather than provoking it further",
               any("provoking it further" in n for n in out.notes))
    check_true("the wait is charged to itself", "waiting" in out.timings)
finally:
    (config.SECURE_CHALLENGE_PAUSE_SECONDS,
     config.SECURE_RETRY_PAUSE_SECONDS) = was
    time.sleep = real_sleep


print("\nA search button that never appears is worth exactly one reload")
# 2026-08-21 05:57, on a real weekend listing. The button did not become
# clickable, and the attempt returned. A warm browser parks on the event page
# for minutes at a time, so a page gone stale is the likeliest explanation and
# reloading it is the obvious response.
page = FakePage(EVENT.url, search_fails=1)
session = FakeSession(page, feed=payload([]))
result = buyer._secure_once(session, EVENT, LISTING)
check("it pressed search twice", page.search_clicks, 2)
check("reloading the page in between", page.reloads, 1)
check_true("and got the search away in the end", page.searched)
check_true("the quantity is set again after the reload — the page resets it",
           session.quantity_calls >= 2)
check_true("and it says what it did", any("reloading" in n for n in result.notes))

print("\nBut only one — a page that is genuinely broken must not eat the window")
page = FakePage(EVENT.url, search_fails=5)
result = buyer._secure_once(FakeSession(page, feed=payload([])), EVENT, LISTING)
check("it stops after the second failure", page.search_clicks, 2)
check("having reloaded once", page.reloads, 1)
check_true("it reports the failure", "could not press search" in (result.reason or ""))
check_true("and says the reload was already tried",
           "even after reloading" in (result.reason or ""))
check("nothing was secured", result.secured, False)
# The seconds spent failing belong to the step that failed. They used to
# vanish: mark() runs after the thing it measures, so a step that raised was
# never marked at all and its time left no trace anywhere.
check_true("the failed step is still timed", "search" in result.timings)


print("\nA panel with no row asks the feed, and RECORDS what it hears")
# The bug this replaces kept the answer in a local variable. Two costs: the
# hold record said still_listed: null when the feed had answered plainly, and
# secure() — which decides whether to go back by reading that field — could
# never see the one case worth going back for.
page = FakePage(EVENT.url, rows_visible=False)
session = FakeSession(page, feed=payload(["lsomethingelse"]))
result = buyer._secure_once(session, EVENT, LISTING)
check("the feed was asked", session.feed_calls, 1)
check("and the answer is on the result, not in a local",
      result.still_listed_after, True)
check("with the ids it named", result.ids_after, ["lsomethingelse"])
check("and the id we were after", result.listing_id, "ly7vs38jkx")
check_true("the reason says the ticket was reachable by hand",
           "reachable by hand" in (result.reason or ""))

print("\n...and distinguishes that from a ticket that really did sell")
page = FakePage(EVENT.url, rows_visible=False)
session = FakeSession(page, feed=payload([]))
result = buyer._secure_once(session, EVENT, LISTING)
check("the feed says nothing is left", result.still_listed_after, False)
check_true("and the reason says so plainly",
           "genuinely sold" in (result.reason or ""))

print("\n...and from not being able to ask at all")
# The third case is not a detail. "Sold" and "we could not tell" call for
# different responses, and reporting the first when the second is true is how
# a fixable problem gets filed as bad luck.
page = FakePage(EVENT.url, rows_visible=False)
session = FakeSession(page, feed=None)
result = buyer._secure_once(session, EVENT, LISTING)
check("the answer is unknown, not False", result.still_listed_after, None)
check_true("and the reason admits it", "unknown" in (result.reason or ""))


print("\nThe dead-end screen tells the same three answers apart")
# Observed live on 2026-08-21 at 11:19. The click lands on
# secure.ticketmaster.ie/error/q404 while the resale endpoint is same-origin
# to www.ticketmaster.ie — so the probe's relative fetch has no valid origin
# and CANNOT answer. Every attempt reaching this screen is in that position,
# and the branch used to report "the resale feed agrees it is no longer there"
# regardless, inventing a confirmation it had never received.


class GonePage(FakePage):
    """A page whose click lands on Ticketmaster's 'sold or removed' screen."""

    def __init__(self, url, feed_origin_ok):
        super().__init__(url, rows_visible=True)
        self.feed_origin_ok = feed_origin_ok
        self.clicked_through = False

    def get_by_text(self, text, exact=False):
        self.clicked_through = True
        return FakeLocator(self, "row")

    def inner_text(self, _sel="body"):
        return ("sorry, these tickets are unavailable the tickets you wanted "
                "have either been sold or removed from sale"
                if self.clicked_through else "")


for label, feed, want_listed, expect in (
    ("could not be asked", None, None, "could NOT be asked"),
    ("agrees it is gone", payload([]), False, "the resale feed agrees"),
    ("still lists it", payload(["lheld"]), True, "somebody else's basket"),
):
    page = GonePage("https://secure.ticketmaster.ie/error/q404", True)
    result = buyer._secure_once(FakeSession(page, feed=feed), EVENT, LISTING)
    check(f"the feed {label}: still_listed_after",
          result.still_listed_after, want_listed)
    check_true(f"the feed {label}: and the reason says so",
               expect in (result.reason or ""))

# The specific regression: an unanswerable probe must never be reported as a
# confirmation that the ticket sold.
page = GonePage("https://secure.ticketmaster.ie/error/q404", False)
result = buyer._secure_once(FakeSession(page, feed=None), EVENT, LISTING)
check("an unanswered probe does not claim the feed agreed",
      "feed agrees" in (result.reason or ""), False)


print("\nA screen that cannot reach the endpoint asks from one that can")
# The dead end is on secure.ticketmaster.ie; the endpoint is same-origin to
# www.ticketmaster.ie. So the relative fetch has the wrong host and CANNOT
# answer — not sometimes, but on every attempt that reaches this screen. The
# forensic that decides "did it sell, or was it never takeable" has therefore
# never run on the path it was built for. A second tab in the same browser
# context carries the same cookies and can ask.


class OffOriginSession(FakeSession):
    """Answers nothing from here, and the real answer from the event page."""

    def __init__(self, page, from_origin):
        super().__init__(page, feed=None)
        self.from_origin = from_origin
        self.origin_calls = 0

    def listings_from_origin(self, event, qty):
        self.origin_calls += 1
        return self.from_origin


page = GonePage("https://secure.ticketmaster.ie/error/q404", False)
session = OffOriginSession(page, payload(["lstillthere"]))
result = buyer._secure_once(session, EVENT, LISTING)
check("it tried from here first", session.feed_calls, 1)
check("then asked from a tab that can reach the endpoint",
      session.origin_calls, 1)
check("and got a real answer at last", result.still_listed_after, True)
check("with the ids", result.ids_after, ["lstillthere"])
check_true("which changes the verdict entirely",
           "somebody else's basket" in (result.reason or ""))

# And the honest unknown survives when even that cannot answer, rather than
# collapsing back into "it sold".
page = GonePage("https://secure.ticketmaster.ie/error/q404", False)
session = OffOriginSession(page, None)
result = buyer._secure_once(session, EVENT, LISTING)
check("a second failure is still an unknown", result.still_listed_after, None)
check("and never a claim that it sold",
      "feed agrees" in (result.reason or ""), False)


print("\nGoing back is only ever offered for a ticket that did not sell")
# secure() gates its retry on still_listed_after. With the field now actually
# set, the retry can fire — which it never has in fourteen attempts.
pauses = []
real_sleep = time.sleep
time.sleep = lambda s: pauses.append(s)
was_pause = config.SECURE_RETRY_PAUSE_SECONDS
was_poll = config.SECURE_RELIST_POLL_SECONDS
try:
    # A tiny but NON-ZERO pause. The wait is no longer a blind sleep — it
    # watches the resale feed and returns the moment the listing comes back
    # (see buyer._wait_for_relist), which costs one XHR per look instead of a
    # whole search per retry. A zero pause therefore correctly does nothing at
    # all, so asking for one and then asserting that time was spent tests the
    # opposite of what it reads as.
    config.SECURE_RETRY_PAUSE_SECONDS = 0.02
    config.SECURE_RELIST_POLL_SECONDS = 0.01
    page = FakePage(EVENT.url, rows_visible=False)
    session = FakeSession(page, feed=payload(["lstillthere"]))
    out = buyer.secure(session, EVENT, LISTING)
    check("it went back for a ticket the feed still lists",
          out.attempts, 1 + config.SECURE_RETRIES)
    check_true("and waited in between for the other basket to lapse",
               len(pauses) >= config.SECURE_RETRIES)

    pauses.clear()
    page = FakePage(EVENT.url, rows_visible=False)
    out = buyer.secure(FakeSession(page, feed=payload([])), EVENT, LISTING)
    check("but not for one that has genuinely gone", out.attempts, 1)
    check("and spends nothing waiting for it", pauses, [])

    # The sequence tells a different story than its last line. Seen twice on
    # 2026-08-21, at 12:20 and 12:25: refused while the feed still listed the
    # very id we tried, then genuinely gone twenty seconds later. Reporting
    # only the final answer calls that a race lost at the click, which is
    # exactly backwards — we never had a race, the ticket was already in
    # somebody's basket and they then paid for it.
    class ThenSold(FakeSession):
        """Still listed on the first ask, gone on every one after."""

        def listings_now(self, event, qty):
            self.feed_calls += 1
            return payload(["lheld"]) if self.feed_calls == 1 else payload([])

    pauses.clear()
    out = buyer.secure(
        ThenSold(FakePage(EVENT.url, rows_visible=False)), EVENT, LISTING)
    check("it went back once", out.attempts, 2)
    check("the last look says it has gone", out.still_listed_after, False)
    check_true("but the sequence is remembered", out.ever_listed_after)
    check_true("and the verdict says it was claimed before we saw it",
               "claimed before we ever saw it" in (out.reason or ""))
    check("not that we lost a race at the click",
          "race being lost at the last step" in (out.reason or ""), False)
finally:
    config.SECURE_RETRY_PAUSE_SECONDS = was_pause
    config.SECURE_RELIST_POLL_SECONDS = was_poll
    time.sleep = real_sleep


print("\nThe wait between attempts is charged to itself, not to the next one")
# mark() measures the gap since the previous mark, and after a retry pause the
# previous mark belongs to the attempt BEFORE the sleep — so the whole pause
# landed on the next attempt's `navigate`, which is the first step it marks.
#
# That is not cosmetic. It read as "navigate 22.5s" with a twenty-second pause
# and "navigate 41.9s" with a forty-second one, on a home connection where a
# single-attempt navigate measures 0.0s — and it was reported as evidence of a
# slow connection for a day before the arithmetic gave it away.
real_sleep = time.sleep
time.sleep = lambda s: pauses.append(s)
was_pause = config.SECURE_RETRY_PAUSE_SECONDS
try:
    config.SECURE_RETRY_PAUSE_SECONDS = 0.0
    pauses.clear()
    out = buyer.secure(
        FakeSession(FakePage(EVENT.url, rows_visible=False),
                    feed=payload(["lstillthere"])), EVENT, LISTING)
    check_true("more than one attempt ran", out.attempts > 1)
    check_true("the wait has a step of its own", "waiting" in out.timings)
    # With navigation faked as instant, navigate must stay near zero however
    # many pauses happened. Before the fix it accumulated one pause per retry.
    check_true("and navigate is not carrying it",
               out.timings.get("navigate", 0.0) < 1.0)
finally:
    config.SECURE_RETRY_PAUSE_SECONDS = was_pause
    time.sleep = real_sleep


print("\nWhere the attempt ended is recorded however it ended")
# The dead-end URL is the only place a direct link to a single listing has
# ever been observed, and a direct link would remove the
# navigate-quantity-search-panel sequence that costs three quarters of every
# attempt. It was captured on one failure path in five, and the two attempts
# recorded since it was added both failed before reaching that one.
class StuckPage(FakePage):
    """A page that reports where it is and refuses to go anywhere else.

    goto() is a no-op here on purpose: the search-button failure reloads, and
    a fake that let the reload change the URL would make the assertion below
    pass by accident — it would be comparing landed_url against whatever the
    reload had just set, rather than proving anything was captured at all.
    """

    def goto(self, url, wait_until=None):
        self.reloads += 1


for label, page, feed in (
    ("when the search button never appeared",
     StuckPage("https://www.ticketmaster.ie/stuck", search_fails=5), payload([])),
    ("when the panel drew no row",
     StuckPage("https://www.ticketmaster.ie/panel", rows_visible=False), payload([])),
):
    result = buyer._secure_once(FakeSession(page, feed=feed), EVENT, LISTING)
    check(f"{label}, the URL is kept", result.landed_url, page.url)

# And the clock stops when the attempt does, rather than running on while the
# failure email is written. `seconds` in the event log is the sum of completed
# steps and is short by exactly the step that failed; this is the honest one.
result = buyer._secure_once(
    FakeSession(FakePage(EVENT.url, search_fails=5), feed=payload([])),
    EVENT, LISTING)
check_true("the attempt records when it finished", result.finished_at is not None)
frozen = result.elapsed
real_sleep(0.05)
check("and the elapsed time stops moving afterwards", result.elapsed, frozen)
check_true("which is at least what the measured steps add up to",
           result.elapsed >= sum(result.timings.values()) - 0.001)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
