"""A lost race must say where its seconds went.

Until 2026-08-20 the best answer available to "why did we lose that ticket?"
was "roughly sixty seconds, probably" — inferred from log lines with
minute-resolution timestamps. Two weekend listings at €366.39 were found and
lost that day against that standard of evidence.

That is not a measurement, and you cannot tune a race against it. Every step
of a securing attempt is now timed, the breakdown goes into the log and into
the failure email, and the next lost ticket arrives as a number instead of a
shrug. It is also what makes "keeping the buying browser warm helped" a fact
rather than an opinion.

Everything here is arithmetic on a HoldResult; no browser and no network.

Run with:  .venv/bin/python tests/test_hold_timings.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import notify  # noqa: E402
from ep_watcher.buyer import HoldResult  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def timed(**steps):
    """A HoldResult whose timings are set directly, without waiting."""
    r = HoldResult()
    for name, seconds in steps.items():
        r.timings[name] = seconds
    return r


print("\nEach mark measures the gap, not the total")
# Gaps rather than cumulative totals, so the steps sum to the elapsed time and
# the expensive one is obvious. Cumulative timings make every step after a
# slow one look slow too.
r = HoldResult()
time.sleep(0.05)
first = r.mark("launch")
time.sleep(0.05)
second = r.mark("navigate")
check_true("the first step is about the time it took", 0.03 <= first <= 0.2)
check_true("and so is the second, not the running total", 0.03 <= second <= 0.2)
check("both steps are recorded", list(r.timings), ["launch", "navigate"])
check_true("and they roughly sum to the elapsed time",
           abs(sum(r.timings.values()) - r.elapsed) < 0.05)

print("\nA repeated step accumulates rather than overwriting")
# A retry is still time spent on that step. Overwriting would flatter exactly
# the step that most needs looking at.
r = HoldResult()
r.mark("navigate")
r.timings["navigate"] = 2.0
time.sleep(0.02)
r.mark("navigate")
check_true("the retry is added to the first attempt", r.timings["navigate"] > 2.0)
check("and it is still one step, not two", list(r.timings), ["navigate"])

print("\nThe summary names the slowest step, because that is the actionable one")
r = timed(launch=14.0, navigate=21.0, quantity=1.0, search=4.0, panel=19.0)
line = r.timing_line()
check_true("every step appears", all(s in line for s in
           ("launch", "navigate", "quantity", "search", "panel")))
check_true("the total is stated", "total 59.0s" in line)
check_true("and the worst offender is called out", "slowest: navigate" in line)

# The specific shape of the 2026-08-20 losses: a cold browser start and a
# navigate that together cost more than half the window.
cold = r.timings["launch"] + r.timings["navigate"]
check_true("cold-start plus navigate is over half the attempt",
           cold > sum(r.timings.values()) / 2)

print("\nNothing measured means nothing printed")
# An attempt that died before it began must not print a heading over an empty
# list, in the log or in the email.
check("no timings, no line", HoldResult().timing_line(), "")
check("and no email block", notify._timing_block(HoldResult()), "")
check_true("but a measured one does produce a block",
           "Where the time went" in notify._timing_block(timed(launch=3.0)))

print("\nThe email block survives an object that cannot be timed")
# secure_failed() is the last thing standing between David and believing in a
# hold that does not exist. It must not raise because a stand-in HoldResult
# from a test, or an older pickled one, has no timings on it.
check("an object with no timing_line is tolerated",
      notify._timing_block(object()), "")


class Awkward:
    def timing_line(self):
        raise RuntimeError("no clock here")


try:
    notify._timing_block(Awkward())
    raised = False
except Exception:
    raised = True
check("a raising timing_line is not caught, so it would be noticed in tests",
      raised, True)

print("\nElapsed measures the whole attempt, including untimed gaps")
r = HoldResult()
time.sleep(0.05)
check_true("elapsed advances without any mark being taken", r.elapsed >= 0.04)
check("while nothing has been attributed to a step", list(r.timings), [])

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
