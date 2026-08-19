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


print("\nThe signed-in check must happen on a loaded page, not on about:blank")
# A freshly started BrowserSession sits on about:blank, which contains neither
# "sign out" nor "my account". Asking whether we are signed in before
# navigating therefore always answered "no", which made the entire feature a
# no-op that would have blamed a login problem on the first real listing.


class RecordingPage:
    def __init__(self):
        self.order = []
        self.body = ""

    def goto(self, url, wait_until=None):
        self.order.append("goto")
        self.body = "Electric Picnic  Sign Out  My Account"

    def inner_text(self, _sel):
        return self.body

    def get_by_role(self, *a, **k):
        raise RuntimeError("stop here — ordering is what this test is about")

    def get_by_text(self, *a, **k):
        raise RuntimeError("stop here")


class OrderProbeSession:
    def __init__(self):
        self._page = RecordingPage()

    @property
    def page(self):
        return self._page

    def signed_in(self):
        self._page.order.append("signed_in")
        return "sign out" in self._page.body.lower()

    def set_quantity(self, qty, result):
        pass


probe = OrderProbeSession()
outcome = buyer.secure(probe, config.EVENTS[0], resale_reading().listings[0])
check("navigates before checking the session", probe.page.order[:2], ["goto", "signed_in"])
check_true("and does not wrongly report being signed out",
           "not signed in" not in outcome.reason)


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

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
