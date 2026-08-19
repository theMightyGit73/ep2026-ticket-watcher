"""Reading the hold countdown off a real checkout page.

This number goes straight into an email telling David how long he has to pay
for a ticket that is already held, so being wrong about it is expensive in
one direction: "you have about sixteen minutes" when the clock says two costs
the ticket, while failing to read it at all costs nothing but a fallback to
config.HOLD_MINUTES_HINT, which the alert then labels as an estimate.

The hazard is that a checkout page is full of times that are not the hold —
the event's own start time most of all. "Sat, 5 Sept 2026, 16:00" parses as a
perfectly plausible sixteen-minute countdown.

Until 2026-08-19 the docstring described three defences and the code
implemented one. It took the SMALLEST clock anywhere in the text rather than
the first one down the page, and it did not skip times landing exactly on the
minute at all. It happened to work on the one page ever captured, because
11:39 was smaller than everything else on it — which is luck, not a rule.
These checks pin the three rules that are now actually implemented.

Run with:  .venv/bin/python tests/test_countdown_reading.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakePage:
    """The only thing read_countdown_minutes asks of a page is its body text."""

    def __init__(self, text, raises=False):
        self._text = text
        self._raises = raises

    def inner_text(self, _selector):
        if self._raises:
            raise RuntimeError("page closed while we were reading it")
        return self._text


def minutes(text, **kw):
    return buyer.read_countdown_minutes(FakePage(text, **kw))


print("\nThe page actually captured on 2026-08-19, mid-hold")
# Reproduced from the description in buyer.py: the countdown alone on its own
# line, printed twice, above the word "Checkout", with the event's own start
# time further down inside a sentence.
REAL = """Electric Picnic 2026

11:39

11:39

Checkout

Weekend Camping
Sat, 5 Sept 2026, 16:00
Section STNDN1
Place Order
Cancel Order
"""
got = minutes(REAL)
check("reads 11:39 as 11.65 minutes", round(got, 2), 11.65)

print("\nRule 1 — a time inside a sentence is never the countdown")
check("the event's start time embedded in a line is ignored",
      minutes("Weekend Camping\nSat, 5 Sept 2026, 16:00\nPlace Order\n"), None)
check("and so is a doors time", minutes("Doors 19:00\nPlace Order\n"), None)

print("\nRule 2 — a time landing exactly on the minute is a clock, not a countdown")
# This is the one that made the old implementation dangerous. A bare 16:00 on
# its own line parses as sixteen minutes, which is entirely plausible for a
# hold, so it would have been reported as the time remaining.
check("a bare 16:00 alone on a line is not read as sixteen minutes",
      minutes("Checkout\n16:00\nPlace Order\n"), None)
check("nor is 05:00", minutes("05:00\nPlace Order\n"), None)
check_true("but 05:01 is a countdown",
           minutes("05:01\nPlace Order\n") is not None)

print("\nRule 3 — the first clock down the page wins, not the smallest")
# The captured page put the countdown at the very top. Preferring the
# smallest match anywhere would hand a stray shorter time further down the
# page priority over the page's own countdown.
two_clocks = "09:45\nCheckout\nSection STNDN1\n02:15\nPlace Order\n"
check("the first is taken even though a smaller one follows",
      round(minutes(two_clocks), 2), 9.75)
check("the smallest-anywhere rule would have said 2.25 — it no longer applies",
      minutes(two_clocks) != 2.25, True)

print("\nBounds and failure — all of which fall back to the estimate")
check(f"anything over {buyer.COUNTDOWN_MAX_MINUTES} minutes is not a hold",
      minutes("45:30\nPlace Order\n"), None)
check("a page with no clock at all reads as nothing",
      minutes("Place Order\nCancel Order\n"), None)
check("empty text reads as nothing", minutes(""), None)
check("a page that raises while being read never raises onward",
      minutes("11:39\n", raises=True), None)

print("\nAnd the whole point: the caller can tell measured from estimated")
# secure() sets minutes_measured only when this returned a number, and the
# alert words the two differently — "the page says 11:39" deserves more trust
# than "you have about ten minutes", which comes from one observation of an
# entirely different event.
result = buyer.HoldResult()
check("a fresh HoldResult does not claim a measurement", result.minutes_measured, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
