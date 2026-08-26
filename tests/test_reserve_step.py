"""The watcher may reserve a ticket. It may never buy one.

Added 2026-08-26, when David authorised the one step that turns a reachable
ticket into a reserved one.

The checkout captured at 00:53 that morning has exactly one forward control,
labelled "Continue To Payment", and Ticketmaster's own warning beside it reads
"Proceed to payment to reserve these tickets". So that button is the
reservation step — there is no separate hold or basket control. Until it was
pressed, the watcher reached that page and stopped, leaving the ticket
takeable by anyone until David got to a laptop.

Pressing it moves the browser one screen closer to a purchase than this
project has ever gone, which is why the boundary is pinned here rather than
left to the reading of whoever next edits buyer.py. The line is exact: press
that one control, by exact name, once, and never press anything again.

Run with:  .venv/bin/python tests/test_reserve_step.py
"""

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


class FakeLocator:
    def __init__(self, name="", visible=True, checked=False):
        self.name, self._visible, self._checked = name, visible, checked
        self.clicked = False

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._visible

    def is_checked(self):
        return self._checked

    def check(self, timeout=None):
        self._checked = True

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    """Serves one button and one checkbox, and records every click."""

    def __init__(self, button_name="Continue To Payment"):
        self.button = FakeLocator(button_name)
        self.box = FakeLocator("I agree to the Ticket Exchange Policy")

    def get_by_role(self, role, name=None, exact=False):
        if role == "checkbox":
            return self.box
        if role == "button":
            if exact and (name or "").lower() != self.button.name.lower():
                return FakeLocator(visible=False)
            return self.button
        return FakeLocator(visible=False)


class R:
    def __init__(self):
        self.notes = []
        self.reserved = False

    def note(self, t):
        self.notes.append(t)


buyer.button_labels = lambda b: [getattr(b, "name", "")]


print("\nIt takes the reserve step, and says so")

page, r = FakePage(), R()
check("the step is taken", buyer._reserve_at_checkout(page, r), True)
check("the button was pressed", page.button.clicked, True)
check("the policy box was ticked first", page.box._checked, True)
check("the result records a reservation", r.reserved, True)
check("and the note says nothing further will be pressed",
      any("STOPPING HERE" in n for n in r.notes), True)


print("\nIt will not press a control that completes a purchase")

# The exact trap this guards: a page that relabels its final purchase button
# with the same words as the reserve control. The exact-name match alone would
# let that through, so NEVER_PRESS is checked against the label as well.
for label in ("Pay Now", "Place Order", "Confirm and Pay", "Complete Purchase"):
    page, r = FakePage(button_name=label), R()
    # Force the name match so only the NEVER_PRESS check can save us.
    page.get_by_role = lambda role, name=None, exact=False, _p=page: (
        _p.box if role == "checkbox" else _p.button)
    check(f"refuses {label!r}", buyer._reserve_at_checkout(page, r), False)
    check(f"and never clicked it", page.button.clicked, False)


print("\nThe switch really switches it off")

was = config.RESERVE_AT_CHECKOUT
try:
    config.RESERVE_AT_CHECKOUT = False
    page, r = FakePage(), R()
    check("no step taken when off", buyer._reserve_at_checkout(page, r), False)
    check("and nothing was clicked", page.button.clicked, False)
    check("and it says why",
          any("switched off" in n for n in r.notes), True)
finally:
    config.RESERVE_AT_CHECKOUT = was


print("\nThe never-press list still covers the purchase vocabulary")

for word in ("pay now", "place order", "confirm"):
    check(f"{word!r} is on the never-press list",
          any(word in n for n in buyer.NEVER_PRESS), True)

# And the one control we DO press is named exactly, not by prefix.
check("the reserve button is matched exactly",
      buyer.RESERVE_BUTTON, "continue to payment")


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All reserve-step checks passed.")
