"""How fast the sweep runs, how it loses that speed, and how it gets it back.

The sweep is the detector that works. It found every weekend listing of
2026-08-20 and 21 ahead of any search. So the rate it runs at is not a tuning
detail — it is the latency between a ticket appearing and anyone knowing,
against listings that are gone inside two minutes.

Three things went wrong with that rate, and all three were invisible.

  * It could only ever get slower. `_interval` was set once at construction
    and doubled on refusal, with nothing anywhere to lower it. Three refusal
    bursts between 21:46 and 03:05 — at hours when nothing is on sale and
    being slow costs nothing — walked it 90 -> 180 -> 360 -> 600 and left it
    there for the morning. The listing at 05:57 was found at that rate. The
    only thing that restored ninety seconds was the watchdog restarting the
    process at 09:50 for an unrelated reason.

  * The ceiling was above the searches. 600s was justified in a comment as
    "still far faster than the searches", and the standard page's peak search
    window is 180-360s. A detector that has quietly become the slowest thing
    in the system is worse than none, because the numbers still report it
    working.

  * `status` and `budget` printed the configured rate. Both would have said
    "every 90s" all that night, which is the one question either exists to
    answer.

Run with:  .venv/bin/python tests/test_sweep_cadence.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, engine, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def payload(picks=(), status=200):
    return {"url": "/api/quickpicks/X/resale", "status": status,
            "data": {"picks": list(picks), "total": len(picks)}}


class FakeSession:
    """Answers every fetch the same way, and counts the asking."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0
        self.asked = []

    def fetch_resale_json(self, event, qty):
        self.calls += 1
        self.asked.append(event.slug)
        return self.answer


def fresh_state():
    return dict(st._defaults())


def sweep_calls(sweep, session, n):
    """Drive the sweep until it has made n calls, making everything due.

    Counts CALLS rather than passes on purpose. A pass asks once per swept
    page, so a helper that counted passes silently measured something
    different the moment a second page joined the sweep — which is exactly
    what happened when config.SWEEP_INSTALMENT was turned back on, and it
    broke the recovery assertions below without any of them being wrong.
    """
    guard = 0
    while session.calls < n and guard < n * 20:
        guard += 1
        sweep._next.clear()
        sweep._resume_at = 0.0
        sweep.run(session, fresh_state())


print("\nThe ceiling has to be faster than the searches it exists to beat")
# This is the invariant the old 600s value violated, and it is the reason the
# number is what it is rather than a round figure someone liked. Asserted
# against the config rather than restated, so changing one without the other
# fails here instead of in the field at three in the morning.
standard = config.EVENTS[0]
mean_peak_gap = (standard.peak_min_seconds + standard.peak_max_seconds) / 2
check_true(
    f"the slowest sweep ({config.RESALE_SWEEP_MAX_SECONDS:.0f}s) still beats the "
    f"mean peak search gap ({mean_peak_gap:.0f}s)",
    config.RESALE_SWEEP_MAX_SECONDS < mean_peak_gap)
check_true("and the ceiling is above the base rate, or backing off does nothing",
           config.RESALE_SWEEP_MAX_SECONDS > config.RESALE_SWEEP_SECONDS)


# The ladder assertions below count CALLS against the recovery threshold, and
# a pass asks once per swept page — so with two pages a pass can step from
# nineteen calls to twenty-one and skip the boundary being tested. The ladder
# has nothing to do with page coverage, so it is measured against a single
# swept page. The coverage rules get their own section further down, which
# uses the real configuration.
_restore = [(e, e.sweep) for e in config.EVENTS]
for _e in config.EVENTS[1:]:
    _e.sweep = False

print("\nRefusals slow it down, to a bound")
sweep = engine.ResaleSweep()
check("it starts at the configured rate",
      sweep._interval, float(config.RESALE_SWEEP_SECONDS))
for _ in range(12):
    sweep._back_off(time.monotonic())
check("and stops slowing at the ceiling",
      sweep._interval, config.RESALE_SWEEP_MAX_SECONDS)


print("\nAnd clean answers win the speed back — the half that was missing")
sweep = engine.ResaleSweep()
sweep._back_off(time.monotonic())
slowed = sweep._interval
check_true("it is slower after a rest", slowed > config.RESALE_SWEEP_SECONDS)

session = FakeSession(payload())
sweep_calls(sweep, session, config.RESALE_SWEEP_RECOVER_AFTER - 1)
check("one short of the threshold changes nothing", sweep._interval, slowed)

sweep_calls(sweep, session, config.RESALE_SWEEP_RECOVER_AFTER)
check("and the threshold halves it", sweep._interval, slowed / 2)
check("the counter starts again", sweep._clean, 0)

# All the way home, and no further: the sweep is not permitted to talk itself
# into being faster than it was asked to be.
sweep = engine.ResaleSweep()
for _ in range(6):
    sweep._back_off(time.monotonic())
session = FakeSession(payload())
for _ in range(40):
    sweep_calls(sweep, session, session.calls + config.RESALE_SWEEP_RECOVER_AFTER)
check("it recovers to the configured rate",
      sweep._interval, float(config.RESALE_SWEEP_SECONDS))
check("and floors there", sweep._interval, float(config.RESALE_SWEEP_SECONDS))


print("\nOnly a real answer counts as clean")
# A refusal, a fetch that resolved against no origin, and a reply whose shape
# could not be read all mean the endpoint told us nothing — and none of them
# is evidence that the current rate is tolerated. Counting them would let a
# sweep that is being refused talk its way back up to the rate being refused.
for label, answer in (("no answer at all", None),
                      ("an unreadable shape", {"status": 200, "data": None})):
    sweep = engine.ResaleSweep()
    sweep._back_off(time.monotonic())
    was = sweep._interval
    sweep_calls(sweep, FakeSession(answer), config.RESALE_SWEEP_RECOVER_AFTER * 2)
    check(f"{label} leaves the rate exactly where it was", sweep._interval, was)

# A refusal is the one that moves in the other direction, so the property is
# "never faster" rather than "unchanged" — being refused repeatedly is what
# backing off is for.
sweep = engine.ResaleSweep()
sweep._back_off(time.monotonic())
was = sweep._interval
sweep_calls(sweep, FakeSession(payload(status=403)),
             config.RESALE_SWEEP_RECOVER_AFTER * 2)
check_true("a refusal never earns the speed back", sweep._interval >= was)
check("it goes the other way, to the ceiling",
      sweep._interval, config.RESALE_SWEEP_MAX_SECONDS)

# Progress is forfeited by a rest, not banked. Twenty clean answers followed
# by a refusal is not nineteen-twentieths of the way to being trusted faster;
# it is evidence the current rate is already too fast.
sweep = engine.ResaleSweep()
sweep._back_off(time.monotonic())
sweep_calls(sweep, FakeSession(payload()), config.RESALE_SWEEP_RECOVER_AFTER - 1)
check_true("progress towards recovery is real", sweep._clean > 0)
sweep._back_off(time.monotonic())
check("and a rest throws it away", sweep._clean, 0)


print("\nRecovering must not fire a burst of calls")
# _back_off clears the per-page clocks, which is safe because it is paired
# with a rest — nothing is due until the rest ends. Doing the same on the way
# UP would make every page due at once and send a spike of calls at the exact
# moment we have decided to ask more often. The point is a higher rate, not a
# spike.
sweep = engine.ResaleSweep()
sweep._back_off(time.monotonic())
sweep_calls(sweep, FakeSession(payload()), config.RESALE_SWEEP_RECOVER_AFTER)
check("nothing is due the instant it speeds up",
      sweep.any_due(time.monotonic()), False)
check_true("but everything is within the new interval",
           sweep.any_due(time.monotonic() + sweep._interval + 1))


for _e, _was in _restore:
    _e.sweep = _was

print("\nThe sweep covers the pages it is told to, not every page watched")
# Its own switch, one rung below `watch`. The refusals scale with how MANY
# pages are swept rather than with which, so this is the lever that reduces
# call volume without switching a page off entirely.
swept = [e.slug for e in engine.ResaleSweep().pages()]
check_true("the standard weekend page is always swept",
           config.EVENTS[0].slug in swept)
check("the instalment page follows its flag",
      config.EVENTS[1].slug in swept, config.EVENTS[1].sweep)
check_true("nothing unwatched is swept",
           all(e.searchable() for e in engine.ResaleSweep().pages()))

# And a page that is not swept is genuinely not asked about.
session = FakeSession(payload())
engine.ResaleSweep().run(session, fresh_state())
check_true("only swept pages are asked about",
           set(session.asked) <= set(swept))

was = config.EVENTS[1].sweep
try:
    config.EVENTS[1].sweep = False
    check_true("switching a page off removes it",
               config.EVENTS[1].slug not in
               [e.slug for e in engine.ResaleSweep().pages()])
    config.EVENTS[1].sweep = True
    check_true("and switching it back on restores it",
               config.EVENTS[1].slug in
               [e.slug for e in engine.ResaleSweep().pages()])
finally:
    config.EVENTS[1].sweep = was


print("\nThe live rate is visible from outside the process")
# It lived only in memory, so `status` and `budget` reported the configured
# value and the degradation had no symptom at all.
sweep = engine.ResaleSweep()
state = fresh_state()
check_true("a fresh sweep publishes its rate", sweep.publish(state))
check("which is the configured one", state["sweep_interval"],
      float(config.RESALE_SWEEP_SECONDS))
check("publishing again writes nothing", sweep.publish(state), False)

sweep._back_off(time.monotonic())
check_true("a rest is published", sweep.publish(state))
check("with the new interval", state["sweep_interval"], sweep._interval)
check("and the rest counted", state["sweep_backoffs"], 1)
check_true("and when it will resume", state["sweep_resting_until"])

interval, backoffs, reported, resting = st.sweep_rate(state)
check("readers see the live interval", interval, sweep._interval)
check("and know it was reported rather than assumed", reported, True)
check("with the rests", backoffs, 1)
check_true("and the rest still running", resting > 0)

# Before any sweep has spoken, the fallback is flagged rather than presented
# as fact. "The sweep says 90" and "nobody has told us anything, 90 is the
# setting" are different claims, and the second is what a stopped watcher
# looks like.
interval, _b, reported, _r = st.sweep_rate(fresh_state())
check("with no watcher running, the setting is used",
      interval, float(config.RESALE_SWEEP_SECONDS))
check("but flagged as not reported", reported, False)

sweep_calls(sweep, FakeSession(payload()), config.RESALE_SWEEP_RECOVER_AFTER)
check_true("a recovery is published too", sweep.publish(state))
check("with the faster interval", state["sweep_interval"], sweep._interval)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
