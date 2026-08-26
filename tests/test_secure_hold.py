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

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

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
    """A button labelled the way real ones are — which is not only by text.

    Rendered text is one of several places a control's name can live. This
    models the others (aria-label, title, value) because Playwright matches
    `get_by_role(name=...)` against the ACCESSIBLE name, which any of them can
    supply. A button carrying one of those and no inner text is precisely the
    case that used to walk straight through the payment guard: it matched the
    allowlist on its accessible name and then presented an empty string to the
    forbidden check.
    """

    def __init__(self, text="", visible=True, **attrs):
        self.text = text
        # aria_label= is nicer to type than **{"aria-label": ...}
        self.attrs = {k.replace("_", "-"): v for k, v in attrs.items()}
        self.visible = visible
        self.clicked = False

    @property
    def accessible_name(self) -> str:
        """Roughly what Playwright would compute and match `name=` against."""
        return (self.attrs.get("aria-label") or self.text
                or self.attrs.get("title") or self.attrs.get("value") or "")

    def is_visible(self, timeout=None):
        return self.visible

    def inner_text(self, timeout=None):
        return self.text

    def get_attribute(self, name, timeout=None):
        return self.attrs.get(name)

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    """Answers get_by_role the way Playwright would, from a fixed button list."""

    def __init__(self, buttons, body=""):
        self.buttons = buttons
        self.body = body

    def get_by_role(self, role, name=None, exact=False):
        # Matched on the accessible name, not the rendered text — the whole
        # point of the distinction being tested below.
        wanted = (name or "").lower()
        for b in self.buttons:
            if wanted in b.accessible_name.lower():
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


print("\nA label the guard cannot see is a label it must not trust")
# The same trap, wearing the disguise that actually worked. Its accessible
# name is "Continue to payment" — which is what the allowlist matched on — but
# it renders no text of its own, so vetting inner_text() alone saw "" and
# found nothing to object to. Every label source has to be checked, not just
# the visible one.
hidden = FakeButton("", aria_label="Continue to payment")
result = buyer.HoldResult()
pressed = buyer._press_one_safe_button(FakePage([hidden]), result)
check("does not press an aria-labelled payment button", hidden.clicked, False)
check("and reports that it pressed nothing", pressed, False)
check_true("and names the label it refused",
           any("continue to payment" in n for n in result.notes))

# The same again through `title` and through an <input type=submit>'s `value`,
# because "the label is not the inner text" has more than one spelling.
for attribute, kwargs in (("title", {"title": "Pay now"}),
                          ("value", {"value": "Place order"})):
    trapped = FakeButton("", **kwargs)
    result = buyer.HoldResult()
    buyer._press_one_safe_button(FakePage([trapped]), result)
    check(f"does not press a payment button labelled by {attribute}",
          trapped.clicked, False)

# A button with no readable label anywhere is refused rather than pressed.
# The allowlist matched it on something, so something names it; if none of
# that reaches us, the honest answer is to leave it alone.
mystery = FakeButton("")
result = buyer.HoldResult()


class _NamelessPage(FakePage):
    """A page whose button matches the allowlist but reads back as blank."""

    def get_by_role(self, role, name=None, exact=False):
        return _First(self.buttons[0]) if name == "continue" else _First(
            FakeButton("", visible=False))


pressed = buyer._press_one_safe_button(_NamelessPage([mystery]), result)
check("does not press an unlabelled button", mystery.clicked, False)
check("and reports that it pressed nothing", pressed, False)
check_true("and says it could not read the label",
           any("nothing readable" in n for n in result.notes))

# And a safe button labelled ONLY by aria-label is still pressed — the fix
# must not have turned the guard into a blanket refusal.
aria_ok = FakeButton("", aria_label="Continue")
result = buyer.HoldResult()
check("presses a safe button labelled by aria-label",
      buyer._press_one_safe_button(FakePage([aria_ok]), result), True)
check("and actually clicked it", aria_ok.clicked, True)

# The label collector itself: every source, best first, no blanks, no repeats.
labelled = FakeButton("Continue", aria_label="Continue", title="Go on")
check("collects each distinct label once, inner text first",
      buyer.button_labels(labelled), ["continue", "go on"])
check("a button with nothing readable yields nothing",
      buyer.button_labels(FakeButton("")), [])


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


print("\nThe hold email, and where it sends him")
mails = {}
import importlib  # noqa: E402

real_notify = importlib.reload(notify)
real_notify._send_email = lambda subject, body: mails.update(subject=subject, body=body) or True
real_notify._push = lambda label, **kw: mails.update(push=kw) or True

hold = buyer.HoldResult(secured=True, minutes_hint=4)
real_notify.secured_hold(resale_reading(), hold)
body = mails["body"]
check_true("names the laptop as the certainty", "LAPTOP DEFINITELY HAS IT" in body)
# A MEASURED countdown must still be quoted — that number is read off the
# page and is worth trusting. An UNMEASURED one must not be: the estimate came
# from a boxing match at Croke Park, and the real checkout of 2026-08-26 had
# no clock at all because nothing was reserved. Quoting a number there invents
# a safety margin that does not exist.
measured = buyer.HoldResult(secured=True, minutes_hint=4, minutes_measured=True)
real_notify.secured_hold(resale_reading(), measured)
check_true("a measured countdown is quoted", "4 MINUTES" in mails["body"].upper())

real_notify.secured_hold(resale_reading(), hold)   # unmeasured
check_true("an unmeasured one is not invented",
           "NO COUNTDOWN ON THE PAGE" in mails["body"])
check_true("and it says to go now", "Go now." in mails["body"])

print("\nThe held alert offers the phone a route, honestly framed")
# Reversed on 2026-08-19. This alert used to say flatly "do NOT try to pick
# this up on your phone", on the reasoning that a basket lives in the session
# that created it. True of a signed-OUT session; possibly wrong for a
# signed-in one, where the cart may be bound to the account server-side.
# Nobody has tested which applies, and the error is asymmetric: withholding a
# link that would have worked costs the ticket every time he is out, while
# offering one that does not work costs a glance at an empty basket.
CHECKOUT = "https://www.ticketmaster.ie/checkout/abc123"

with_link = buyer.HoldResult(secured=True, minutes_hint=11, minutes_measured=True,
                             checkout_url=CHECKOUT)
without_link = buyer.HoldResult(secured=True, minutes_hint=10)

body = real_notify._where_to_finish(with_link)
check_true("the link is offered", CHECKOUT in body)
check_true("and offered to the phone first", body.index("PHONE") < body.index("LAPTOP"))
check_true("with the sign-in precondition stated", "signed in" in body)
check_true("and the empty-basket case handled", "EMPTY BASKET" in body.upper())
check_true("the laptop is still named as the certainty", "DEFINITELY HAS IT" in body)

body = real_notify._where_to_finish(without_link)
check_true("with no link, the laptop is the whole answer", "LAPTOP" in body.upper())
check_true("and it says why there is no link", "only way in" in body)
check("no stray empty URL is printed", "https://" in body, False)

print("\nAnd the push carries it, because the push is what reaches him outside")
mails.clear()
real_notify.secured_hold(resale_reading(), with_link)
check_true("push click-through is the checkout", mails["push"].get("click") == CHECKOUT)
check_true("and the title says to tap", "TAP" in mails["push"]["title"])
check_true("email carries it too", CHECKOUT in mails["body"])

mails.clear()
real_notify.secured_hold(resale_reading(), without_link)
check("no link means no click-through", mails["push"].get("click"), None)
check_true("and the title sends him to the laptop", "LAPTOP" in mails["push"]["title"])

# Whatever else changes, this must not: the watcher never pays.
check_true("the email still promises it will not pay",
           "not pay" in mails["body"] or "stops at the basket" in mails["body"])


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
