"""Securing a listing must hold it and never, ever pay for it.

Added 2026-08-19, when David widened the watcher's scope from "tell me" to
"grab it and hold it, then tell me". The line he drew is exact: the watcher
puts a ticket in a basket under his account and stops. He enters the payment
details himself, on the machine holding the basket.

That line is the thing these checks defend. Everything here runs without a
browser and without the network — the flow is exercised against fake pages,
because the property being tested is which buttons the code is willing to
press, and that must be provable without a live listing to gamble with.

Run with:  .venv/bin/python tests/test_secure_hold.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer, config, engine, notify  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def resale_reading(slug="weekend-camping"):
    r = Reading(source="browser")
    r.event_slug = slug
    r.event_name = "Electric Picnic 2026 - Weekend Camping"
    r.event_url = config.EVENTS[0].url
    r.primary = UNAVAILABLE
    r.resale = AVAILABLE
    r.listings.append(
        Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                listing_id="ly7vs38jkx", section="STNDN1")
    )
    return r


print("\nThe payment guard — the whole point of the feature")
# An allowlist plus a denylist. The denylist is checked first so that a page
# labelling its payment control "Continue to payment" cannot slip through on
# the strength of "continue" being permitted.
for label in ("Pay now", "Place order", "Confirm order", "Proceed to checkout",
              "Continue to payment", "Complete purchase", "PAY €366.39"):
    check_true(f"refuses {label!r}", buyer.is_forbidden(label))

for label in ("Continue", "Next", "Get tickets", "Select"):
    check(f"allows {label!r}", buyer.is_forbidden(label), False)

check_true("no payment word is in the allowlist",
           not any(buyer.is_forbidden(b) for b in buyer.SAFE_BUTTONS))


print("\nA fake page must not be able to trick it into paying")


class FakeButton:
    def __init__(self, label, visible=True):
        self.label = label
        self.visible = visible
        self.clicked = False

    def is_visible(self, timeout=None):
        return self.visible

    def inner_text(self, timeout=None):
        return self.label

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    """Answers get_by_role the way Playwright would, from a fixed button list."""

    def __init__(self, buttons, body=""):
        self.buttons = buttons
        self.body = body

    def get_by_role(self, role, name=None, exact=False):
        wanted = (name or "").lower()
        for b in self.buttons:
            if wanted in b.label.lower():
                return _First(b)
        return _First(FakeButton("", visible=False))

    def inner_text(self, _sel):
        return self.body


class _First:
    def __init__(self, button):
        self.button = button
        self.first = button


# The nightmare case: the only visible control is a payment button whose label
# happens to contain an allowed word.
trap = FakeButton("Continue to payment")
result = buyer.HoldResult()
pressed = buyer._press_one_safe_button(FakePage([trap]), result)
check("does not press a disguised payment button", trap.clicked, False)
check("and reports that it pressed nothing", pressed, False)
check_true("and says why in the notes",
           any("payment control" in n for n in result.notes))

# The ordinary case: a plain Continue is fine.
ok = FakeButton("Continue")
result = buyer.HoldResult()
check("presses a safe button", buyer._press_one_safe_button(FakePage([ok]), result), True)
check("and actually clicked it", ok.clicked, True)


print("\nBasket detection is positive-only")
from ep_watcher.sources.browser import BASKET_MARKERS  # noqa: E402

check("an empty page is not a basket", buyer._basket_is_live(FakePage([], ""), BASKET_MARKERS), False)
check("an error page is not a basket",
      buyer._basket_is_live(FakePage([], "this ticket is no longer available"), BASKET_MARKERS),
      False)
check_true("a real basket is",
           buyer._basket_is_live(FakePage([], "Time left to complete 04:59"), BASKET_MARKERS))
# A hold that cannot be seen must never be claimed. Reporting one that is not
# there sends David to a screen with nothing on it while the listing sells.
check("HoldResult defaults to not secured", buyer.HoldResult().secured, False)


print("\nWhen the feature is off, nothing happens at all")
calls = []
config.SECURE_ON_FIND = False
notify.secured_hold = lambda r, h: calls.append("secured")
notify.secure_failed = lambda r, h: calls.append("failed")
engine._maybe_secure(resale_reading())
check("switched off means no browser and no alert", calls, [])

print("\nWhen it is on, it only ever fires for resale")
config.SECURE_ON_FIND = True
# Primary stock reserves itself as a side effect of the search the watcher
# already does, so opening a second signed-in browser for it would be asking
# for a ticket that is already held.
primary_only = resale_reading()
primary_only.resale = UNAVAILABLE
primary_only.primary = AVAILABLE
primary_only.listings = [Listing("General Admission", "€310.50", "primary")]
calls.clear()
engine._maybe_secure(primary_only)
check("primary-only find does not open the buying browser", calls, [])

# An unknown event slug must not be guessed at — securing the wrong page
# would put the wrong ticket in his basket.
calls.clear()
stray = resale_reading(slug="not-a-real-event")
engine._maybe_secure(stray)
check("an unrecognised event is refused", calls, [])


print("\nA doubtful sign-in must not stop the attempt")
# Two bugs lived here in one day, and the second was caused by fixing the
# first badly.
#
# The original code asked "are we signed in?" before navigating, so it read
# about:blank and always answered no — the whole feature was a no-op. The fix
# was to navigate first. But on 2026-08-19 the check ITSELF was shown to be
# worthless: none of the nine real page captures contains "sign out", "my
# account" or even "sign in", because Ticketmaster renders no account text
# that Playwright's flattened inner_text can see. Navigating first simply
# meant reading a real page and still getting the wrong answer.
#
# So secure() no longer consults the page about this at all, and no longer
# refuses on it. A signed-out attempt bounces off a login wall, holds
# nothing, and says so — the same outcome as refusing, without the chance of
# being wrong. The availability alert has already gone out either way.


class RecordingPage:
    def __init__(self):
        self.order = []

    def goto(self, url, wait_until=None):
        self.order.append("goto")

    def inner_text(self, _sel):
        self.order.append("read_text")
        return "Electric Picnic 2026 - Weekend Camping"   # no account text, as in life

    def get_by_role(self, *a, **k):
        self.order.append("interact")
        raise RuntimeError("stop here — what happens before this is the point")

    def get_by_text(self, *a, **k):
        self.order.append("interact")
        raise RuntimeError("stop here")


class ProbeSession:
    def __init__(self):
        self._page = RecordingPage()

    @property
    def page(self):
        return self._page

    def set_quantity(self, qty, result):
        pass


probe = ProbeSession()
outcome = buyer.secure(probe, config.EVENTS[0], resale_reading().listings[0])

check("it navigates first", probe.page.order[0], "goto")
check_true("and goes on to interact with the page", "interact" in probe.page.order)
# The failure it reports must be about the page, never about the login — a
# session it cannot verify is not a session it may assume is broken.
check_true("it does not blame a login it cannot actually check",
           "not signed in" not in (outcome.reason or ""))
check("and no hold is claimed", outcome.secured, False)


print("\nA failed hold must still tell him, and must not claim a hold")
config.SECURE_ON_FIND = True
calls.clear()
sent = {}
notify.secured_hold = lambda r, h: calls.append("secured")
notify.secure_failed = lambda r, h: (calls.append("failed"),
                                     sent.update(reason=getattr(h, "reason", "")))


class ExplodingSession:
    def __init__(self, *a, **k):
        raise RuntimeError("no Chrome here")


buyer.BuySession = ExplodingSession
engine._maybe_secure(resale_reading())
check("a browser that will not start still alerts", calls, ["failed"])
check_true("and the reason reaches the alert", "no Chrome here" in sent.get("reason", ""))


print("\nThe hold email must not send him to his phone")
mails = {}
notify._send_email = lambda subject, body: mails.update(subject=subject, body=body) or True
notify._push = lambda label, **kw: mails.update(push=kw) or True
# Restore the real implementations the fakes above replaced.
import importlib  # noqa: E402

real_notify = importlib.reload(notify)
real_notify._send_email = lambda subject, body: mails.update(subject=subject, body=body) or True
real_notify._push = lambda label, **kw: mails.update(push=kw) or True

hold = buyer.HoldResult(secured=True, minutes_hint=4)
real_notify.secured_hold(resale_reading(), hold)
body = mails["body"]
check_true("says go to the laptop", "GO TO THE MACHINE" in body)
check_true("warns the phone will not work", "phone" in body.lower())
check_true("carries the countdown", "4 minutes" in body)
check_true("carries the section and price", "STNDN1" in body and "366.39" in body)
check_true("promises it will not pay", "will not pay" in body.lower())
# No click-through on the push: a link is the wrong action here, because the
# basket belongs to the browser that made it.
check("push offers no link to open", "click" in mails["push"], False)
check_true("push is urgent", mails["push"]["priority"] == "urgent")

real_notify.secure_failed(resale_reading(), buyer.HoldResult(reason="listing vanished"))
check_true("the failure email says there is no hold", "NO hold" in mails["body"])
check_true("and passes the reason through", "listing vanished" in mails["body"])

print("\nThe dead-end screen is recognised, from a real observation")
# Captured by David on 2026-08-19 from a different event ("Amble", Live at the
# Docklands) — the same Ticketmaster interface. This is exactly the experience
# he described on the Electric Picnic listings: the row is still on the page,
# and clicking it lands here.
GONE_PAGE = """Amble
Thu, Aug 20, 2026, 7:00 PM
Live at the Docklands, Limerick, IE
Over 18s - ID Required.
Ticket Type
Full Price Ticket
Section
STNDNG
Sorry, these tickets are unavailable
The tickets you wanted have either been sold or removed from sale.
Find More Tickets
18406b13-b763-4a89-bae2-204d0e85bad2"""


class TextPage:
    def __init__(self, text):
        self.text = text

    def inner_text(self, _sel=None):
        return self.text


gone = TextPage(GONE_PAGE)
check_true("the dead end is recognised",
           buyer._page_says(gone, buyer.LISTING_GONE_MARKERS))
# The same capture proves the click-through reached the listing's own page —
# "Ticket Type" and "Section" are both on it. That is the first direct
# evidence that clicking a resale row leads anywhere at all.
check_true("and so is having reached the listing page",
           buyer._page_says(gone, buyer.LISTING_DETAIL_MARKERS, all_of=True))
check("a basket is NOT claimed on it",
      buyer._basket_is_live(gone, ("time left to complete", "your tickets are reserved")),
      False)

# all_of matters: "section" alone appears on the search results, so an any-of
# rule would call every page the listing detail page.
results_page = TextPage("Verified Resale Ticket\nSection STNDN1\n366.39")
check("the search results are not mistaken for the listing page",
      buyer._page_says(results_page, buyer.LISTING_DETAIL_MARKERS, all_of=True), False)

check("a page that cannot be read says nothing",
      buyer._page_says(object(), buyer.LISTING_GONE_MARKERS), False)


print("\nThe dead end's only button must never be pressed")
# "Find More Tickets" restarts the search. Pressing it would throw away the
# listing detail page and spend the rest of the 45-second window going round
# a loop instead of reporting the truth.
check_true("Find More Tickets is forbidden", buyer.is_forbidden("Find More Tickets"))
check_true("case and spacing do not matter", buyer.is_forbidden("  find more tickets "))
# And it must not sneak in via the allowlist's substring matching.
for safe in buyer.SAFE_BUTTONS:
    check(f"the allowlist entry {safe!r} does not match it",
          safe in "find more tickets", False)
# The payment guards still hold.
for bad in ("Pay now", "Continue to payment", "Place Order", "Checkout", "Purchase"):
    check_true(f"{bad!r} is still refused", buyer.is_forbidden(bad))


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
