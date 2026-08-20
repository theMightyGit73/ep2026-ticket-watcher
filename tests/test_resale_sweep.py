"""The cheap look between the expensive ones, and everything it must not do.

Added 2026-08-20, after a day of measurement showed the race was not being
lost where anyone assumed. Weekend Camping was searched 30 times that day at a
mean gap of 6.5 minutes; every securing attempt that reached Ticketmaster
found the listing already gone; and the attempt at 11:48 went from detection
to clicking the listing row in under sixty seconds. The watcher is not slow to
react. It is slow to LOOK — a listing had been live about three and a quarter
minutes on average before a search found it.

So the sleep between searches becomes a watch. `fetch_resale_json` already
asks the resale endpoint from inside the live page, carrying its cookies and
origin, and the endpoint's own response says `max-age=15`.

Most of what is tested here is restraint, because that is where the risk is. A
new source of requests against a rate limit that has blocked this connection
twenty times must stay silent when it finds nothing, stop when it is refused,
and never disturb the searches underneath it.

Run with:  .venv/bin/python tests/test_resale_sweep.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


LISTING = {"section": "STNDN1", "description": "WEEKEND CAMPING",
           "originalPrice": {"totalPrice": 366.39}, "resaleListingId": "labc123"}


def payload(picks, status=200):
    return {"url": "/api/quickpicks/X/resale", "status": status,
            "data": {"picks": picks, "total": len(picks)}}


class FakeSession:
    """Answers fetch_resale_json from a script, and counts the asking."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def fetch_resale_json(self, event, qty):
        self.calls += 1
        return self.answers.pop(0) if self.answers else payload([])


handled = []
real_handle = engine.handle
engine.handle = lambda reading, state: handled.append(reading)


def fresh_state():
    return dict(st._defaults())


print("\nAn empty sweep does nothing at all")
# The overwhelmingly common case. It must not count a check, write state, move
# the heartbeat, publish a beacon or send anything — every health number in
# the system would mean something different if a sweep every ninety seconds
# were folded into them.
sweep = engine.ResaleSweep()
state = fresh_state()
before = dict(state)
session = FakeSession([payload([]) for _ in config.EVENTS])
found = sweep.run(session, state)
check("nothing is returned", found, None)
check("nothing was handled", handled, [])
check("state is untouched", state, before)
check_true("but it did ask", session.calls >= 1)


print("\nA listing is handed to the ordinary machinery")
# There must not be a second alerting path to keep in step with the first.
handled.clear()
sweep = engine.ResaleSweep()
state = fresh_state()
session = FakeSession([payload([LISTING])])
found = sweep.run(session, state)
check_true("a reading came back", found is not None)
check("resale is AVAILABLE", found.resale, AVAILABLE)
check("it names the event", found.event_slug, config.EVENTS[0].slug)
check("and handle() was given it", len(handled), 1)
check_true("the source says how it was seen", "sweep" in found.source)
check_true("and the note says how fresh the reading is",
           any("sweep" in n for n in found.notes))


print("\nIt must not overwrite what it does not know")
# record_success() stores whatever the reading holds. A sweep has no opinion
# about the box office, so an UNKNOWN primary would make the hourly email
# report that primary had gone unknown every time a listing appeared.
handled.clear()
state = fresh_state()
st.event_state(state, config.EVENTS[0].slug)["last_primary"] = UNAVAILABLE
sweep = engine.ResaleSweep()
found = sweep.run(FakeSession([payload([LISTING])]), state)
check("the known primary is carried forward", found.primary, UNAVAILABLE)


print("\nRefusals stop it, and stop it for good")
# A sweep being refused is not finding tickets. It is only adding evidence
# that this client asks too often, which is the opposite of its job.
handled.clear()
sweep = engine.ResaleSweep()
state = fresh_state()
for attempt in range(config.RESALE_SWEEP_MAX_REFUSALS):
    sweep._next.clear()          # make it due again
    sweep.run(FakeSession([payload([], status=403)]), state)
check("it gives up after the limit", sweep.stopped, True)
sweep._next.clear()
blocked = FakeSession([payload([LISTING])])
check("and asks nothing further", sweep.run(blocked, state), None)
check("not even one more call", blocked.calls, 0)

# A good answer clears the count, so an isolated refusal cannot accumulate
# across an entire fortnight into a shutdown.
sweep = engine.ResaleSweep()
sweep.run(FakeSession([payload([], status=403)]), fresh_state())
check("one refusal is counted", sweep._refusals, 1)
sweep._next.clear()
sweep.run(FakeSession([payload([])]), fresh_state())
check("and a good answer resets it", sweep._refusals, 0)


print("\nIt yields to the two states that mean 'stop asking'")
handled.clear()
sweep = engine.ResaleSweep()
resting = fresh_state()
st.note_backoff(resting, 1800)
quiet = FakeSession([payload([LISTING])])
check("silent while backing off from a 403", sweep.run(quiet, resting), None)
check("and sends no request", quiet.calls, 0)

sweep = engine.ResaleSweep()
holding = fresh_state()
st.note_hold(holding, 10, event_slug=config.EVENTS[0].slug, priority=100)
quiet = FakeSession([payload([LISTING])])
check("silent while a ticket is held", sweep.run(quiet, holding), None)
check("and sends no request", quiet.calls, 0)


print("\nThe switch, and the clock")
was = config.RESALE_SWEEP
try:
    config.RESALE_SWEEP = False
    off = FakeSession([payload([LISTING])])
    check("EP_RESALE_SWEEP=0 asks nothing", engine.ResaleSweep().run(off, fresh_state()), None)
    check("and really sends nothing", off.calls, 0)
finally:
    config.RESALE_SWEEP = was

sweep = engine.ResaleSweep()
now = time.monotonic()
check("a fresh sweep is due immediately", sweep.any_due(now), True)
sweep.run(FakeSession([payload([]) for _ in config.EVENTS]), fresh_state())
check("and not again straight away", sweep.any_due(time.monotonic()), False)
check_true("it comes due again within its interval",
           sweep.any_due(time.monotonic() + config.RESALE_SWEEP_SECONDS + 1))

# An expired page is never swept — the same rule due_events() follows.
expired = [e for e in config.EVENTS if e.expired("2999-01-01")]
check_true("every page expires eventually", len(expired) >= 1)

print("\nIt can prove it is alive, because silence must not be ambiguous")
# A sweep that is quietly failing looks exactly like a sweep that is finding
# nothing. The realistic failure is mundane: the fetch is relative to the
# page's origin, so a browser parked anywhere but ticketmaster.ie returns None
# every time and says so to nobody.
sweep = engine.ResaleSweep()
state = fresh_state()
sweep.run(FakeSession([payload([]) for _ in config.EVENTS]), state)
check_true("calls are counted", sweep.calls >= 1)
check("and answers too", sweep.answers, sweep.calls)
check("with the empty ones tallied separately", sweep.unavailable, sweep.calls)


class DeadSession:
    """A browser parked off-origin: the fetch resolves against nothing."""

    def __init__(self):
        self.calls = 0

    def fetch_resale_json(self, event, qty):
        self.calls += 1
        return None


sweep = engine.ResaleSweep()
dead = DeadSession()
sweep.run(dead, fresh_state())
check_true("a dead sweep still counts the attempt", sweep.calls >= 1)
check("but records no answer", sweep.answers, 0)
check_true("so the two cases are distinguishable", sweep.calls != sweep.answers)

engine.handle = real_handle
print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
