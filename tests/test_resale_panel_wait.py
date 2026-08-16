"""Knowing when the resale panel is actually READABLE, not merely answered.

This exists because of a regression that cost real coverage. The watcher used
to decide the panel had arrived by polling the rendered text; it was changed
to watch for the network call that fills the panel instead. The reasoning was
sound — the call fires even when the panel is empty, which the text cannot
tell you early — but a Playwright `response` event fires when the response
*headers* arrive, and the panel is painted some way after that. The caller
read the page in the gap and recorded a perfectly good poll as resale-blind.

Measured on the live logs for 2026-08-16, split at the restart that deployed
it: resale unreadable on 10/80 polls before, 22/74 after. 12% to 30%.

The fix keeps the network call for the thing it is genuinely good at —
knowing how long to stay patient — and lets the DOM decide. These checks pin
that, and the invariant underneath it: whenever the waiter says the panel is
readable, the parser must agree. While those two disagreed, a poll could be
declared readable and then parsed as blind, with nothing in the log to say
why.

Everything below runs on a fake clock, so it is instant and never flaky.

Run with:  .venv/bin/python tests/test_resale_panel_wait.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


# ── A clock we control ───────────────────────────────────────────────────────

class FakeClock:
    """Stands in for the `time` module inside browser.py.

    Real sleeps would make this suite take a minute and still be timing
    dependent. Advancing a counter on every sleep() makes the same code paths
    deterministic and instant.
    """

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedPage(browser.BrowserSession):
    """A page whose text changes as the clock advances.

    `script` is [(seconds_from_start, text)]; the latest entry that is due
    wins. `response_at` is when the /api/quickpicks/…/resale call answers,
    or None for a search where it never does.

    Subclassed from the real session, with only the two Chrome-touching
    things replaced, so the waiting and parsing under test are the genuine
    methods. __init__ deliberately does not call super(): there is no browser
    to build, and _resale_response is a computed property here rather than an
    attribute the listener writes.
    """

    def __init__(self, clock, script, response_at=None, status=200):
        self.clock = clock
        self.start = clock.now
        self.script = sorted(script)
        self.response_at = response_at
        self.status = status

    @property
    def elapsed(self):
        return self.clock.now - self.start

    @property
    def _resale_response(self):
        if self.response_at is None or self.elapsed < self.response_at:
            return None
        return {
            "url": "https://www.ticketmaster.ie/api/quickpicks/18006314BD813D3E/resale",
            "status": self.status,
        }

    def visible_text(self):
        text = ""
        for at, body in self.script:
            if self.elapsed >= at:
                text = body
        return text


# ── Page states, taken from the real captures in test_resale_parsing.py ──────

REJECTED = """There aren't enough tickets to complete your request
Please update the quantity or ticket type to see available tickets.
Search Again"""

# The panel exists but has not painted its heading yet — only the generic
# "Other Options" wrapper. This is the state the old waiter accepted and the
# parser did not.
OTHER_OPTIONS_ONLY = REJECTED + "\nOther Options"

PANEL_EMPTY = REJECTED + """
Other Options
Verified Resale Tickets
Resale Tickets will appear below when they are available.
eTickets FREE"""

PANEL_WITH_LISTING = PANEL_EMPTY + """
Section STNDN1
Verified Resale Ticket
WEEKEND CAMPING
€366.39 each"""


def run(page, **kw):
    """The production sequence: wait, then read the page, then parse it.

    Deliberately tests the waiter and the parser together — the bug lived in
    the handover between them, not in either one alone.
    """
    readable, why = browser.BrowserSession._await_resale_panel(page, **kw)
    reading = Reading(source="browser")
    text = browser._normalise(page.visible_text())
    browser.BrowserSession._parse_resale(page, text, reading)
    return readable, why, reading


print("\nA panel already on the page is readable at once")

clock = FakeClock()
browser.time = clock
page = ScriptedPage(clock, [(0, PANEL_EMPTY)], response_at=0)
readable, why, reading = run(page, timeout_s=25, render_s=8, settle_s=2)
check("reported readable", readable, True)
check("and the parser agrees it rendered", reading.resale, UNAVAILABLE)
check("finding no listings in it", len(reading.listings), 0)

print("\nThe regression: the call answers before the panel paints")
# This is the exact shape of the bug. The response lands at 1s, the panel a
# second and a half later. Returning at the response recorded this as blind.

clock = FakeClock()
browser.time = clock
page = ScriptedPage(
    clock,
    [(0, REJECTED), (2.5, PANEL_EMPTY)],
    response_at=1.0,
)
readable, why, reading = run(page, timeout_s=25, render_s=8, settle_s=2)
check("waits for the paint, not the headers", readable, True)
check("so the poll is NOT resale-blind", reading.resale, UNAVAILABLE)
check("which is a real answer, not a shrug", reading.resale == UNKNOWN, False)

print("\nAnd a listing that paints a beat after the heading is still seen")
# The heading arrives before the rows. Parsing on the heading alone would
# read AVAILABLE as "rendered, no listings" — a confident wrong answer, which
# is worse than being blind, because better_status() ranks UNAVAILABLE above
# UNKNOWN and it would win the merge.

clock = FakeClock()
browser.time = clock
page = ScriptedPage(
    clock,
    [(0, REJECTED), (1.2, PANEL_EMPTY), (2.0, PANEL_WITH_LISTING)],
    response_at=1.0,
)
readable, why, reading = run(page, timeout_s=25, render_s=8, settle_s=3)
check("readable", readable, True)
check("and the listing is found", reading.resale, AVAILABLE)
check("not misread as an empty panel", len(reading.listings), 1)
check_true("the reason says listings were seen", "listing" in why)

print("\nA call that answers and never paints is reported, not invented")

clock = FakeClock()
browser.time = clock
page = ScriptedPage(clock, [(0, REJECTED)], response_at=1.0, status=200)
readable, why, reading = run(page, timeout_s=25, render_s=2, settle_s=2)
check("not readable", readable, False)
check("the poll is honestly blind", reading.resale, UNKNOWN)
check_true("and says the panel did not render", "did not render" in why)
check_true("naming the HTTP status", "200" in why)
# It must not sit out the whole timeout waiting for a paint that is not
# coming — that is 25s of a poll interval spent on a settled question.
check("gives up on the render grace, not the full timeout", clock.now - 1000.0 <= 5.0, True)

print("\nNo resale call at all is a different failure, and says so")

clock = FakeClock()
browser.time = clock
page = ScriptedPage(clock, [(0, REJECTED)], response_at=None)
readable, why, reading = run(page, timeout_s=3, render_s=8, settle_s=2)
check("not readable", readable, False)
check_true("and says no call was made", "no resale call" in why)
check("still UNKNOWN, never 'no tickets'", reading.resale, UNKNOWN)

print("\nThe waiter and the parser must agree on what 'readable' means")
# The old waiter accepted a bare "Other Options"; the parser required the
# "Verified Resale" heading. So it could return True and then be parsed as
# blind, producing a resale-blind poll with nothing in the log to explain it.

clock = FakeClock()
browser.time = clock
page = ScriptedPage(clock, [(0, OTHER_OPTIONS_ONLY)], response_at=0)
readable, why, reading = run(page, timeout_s=25, render_s=2, settle_s=1)
check("a half-painted panel is not called readable", readable, False)
check("matching the parser's own verdict", reading.resale, UNKNOWN)

print("\nThe invariant, over every scenario above")
# Whenever the waiter says readable, the parser must produce a real answer.
# This is the property the regression violated, stated once, directly.

scenarios = [
    ("panel present", [(0, PANEL_EMPTY)], 0),
    ("panel late", [(0, REJECTED), (2.5, PANEL_EMPTY)], 1.0),
    ("listing late", [(0, REJECTED), (1.2, PANEL_EMPTY), (2.0, PANEL_WITH_LISTING)], 1.0),
    ("never paints", [(0, REJECTED)], 1.0),
    ("no call", [(0, REJECTED)], None),
    ("half painted", [(0, OTHER_OPTIONS_ONLY)], 0),
]
broken = []
for name, script, response_at in scenarios:
    clock = FakeClock()
    browser.time = clock
    page = ScriptedPage(clock, script, response_at=response_at)
    readable, why, reading = run(page, timeout_s=6, render_s=2, settle_s=1)
    if readable and reading.resale == UNKNOWN:
        broken.append(name)
    if not readable and reading.resale != UNKNOWN:
        broken.append(f"{name} (blind but answered)")
check("readable always means the parser can answer", broken, [])

print("\nEvery outcome carries a reason worth reading in the log")
for name, script, response_at in scenarios:
    clock = FakeClock()
    browser.time = clock
    page = ScriptedPage(clock, script, response_at=response_at)
    _, why = browser.BrowserSession._await_resale_panel(
        page, timeout_s=6, render_s=2, settle_s=1
    )
    check(f"{name} explains itself", bool(why and len(why) > 10), True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
