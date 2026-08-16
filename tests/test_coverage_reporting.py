"""Can the watcher see, as opposed to merely run?

Liveness and coverage are different questions and the watcher used to answer
only the first. A poll can complete, report a confident UNAVAILABLE on
primary, and have learned nothing at all about resale — because the search
resolved before the resale panel rendered, or because the browser was
blocked and only the free API answered.

Measured over the first day of running: 9 polls out of 59 ended with "no
resale panel — the search may not have completed", and nothing anywhere
surfaced that. Resale is the market a ticket has actually turned up on for
this event, so a watcher that is blind to it one poll in six is materially
worse than its own health checks claimed.

Run with:  .venv/bin/python tests/test_coverage_reporting.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, UNKNOWN, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def poll(state, resale):
    st.note_resale_visibility(state, Reading(source="t", resale=resale))


print("\nA fresh state has not measured anything, and must not pretend it has")

s = dict(st._defaults())
severity, headline = st.resale_visibility(s)
check("severity is 'unknown', not 'ok'", severity, "unknown")
check("and it says so", "not measured yet" in headline, True)

print("\nAn old state file must not read as a flawless record")

# The shape of a state file written before any of this was tracked: plenty of
# polls, no resale counters. Dividing by checks_total would render 0 blind
# out of 57 as a perfect 100%, which is a health check flattering itself.
s = dict(st._defaults())
s["checks_total"] = 57
check("a large check count alone proves nothing",
      st.resale_visibility(s)[0], "unknown")

print("\nOnce it is measuring, the rate is reported honestly")

s = dict(st._defaults())
for _ in range(st.MIN_RESALE_SAMPLE):
    poll(s, UNAVAILABLE)
check("all readable is OK", st.resale_visibility(s)[0], "ok")
check("counted every poll", s["resale_checks_total"], st.MIN_RESALE_SAMPLE)
check("and none as blind", s["resale_blind_total"], 0)

s = dict(st._defaults())
for _ in range(19):
    poll(s, UNAVAILABLE)
poll(s, UNKNOWN)
check("the odd blind poll is tolerated", st.resale_visibility(s)[0], "ok")

# One in ten is the line, because a blind poll is close to a whole missed
# chance: the listing observed on this event lived about one poll interval.
s = dict(st._defaults())
for _ in range(st.MIN_RESALE_SAMPLE * 9 // 10):
    poll(s, UNAVAILABLE)
for _ in range(st.MIN_RESALE_SAMPLE - st.MIN_RESALE_SAMPLE * 9 // 10):
    poll(s, UNKNOWN)
check("one in ten is worth saying out loud", st.resale_visibility(s)[0], "watch")

# The rate actually observed in production on 2026-08-14.
s = dict(st._defaults())
for _ in range(50):
    poll(s, UNAVAILABLE)
for _ in range(9):
    poll(s, UNKNOWN)
severity, headline = st.resale_visibility(s)
check("the real observed rate raises a warning", severity, "watch")
check("and reports the fraction", "50/59" in headline, True)

s = dict(st._defaults())
for _ in range(st.MIN_RESALE_SAMPLE):
    poll(s, UNKNOWN)
for _ in range(3):
    poll(s, UNAVAILABLE)
check("mostly blind is a failure", st.resale_visibility(s)[0], "bad")

print("\nA real find is never counted as blindness")

s = dict(st._defaults())
poll(s, AVAILABLE)
check("an available reading is a reading", s["resale_blind_total"], 0)
check("and counts toward the denominator", s["resale_checks_total"], 1)

print("\nThe hourly counters reset, the lifetime ones do not")

s = dict(st._defaults())
for _ in range(3):
    poll(s, UNKNOWN)
st.note_degraded(s, ["browser"])
check("coverage before reset", st.coverage(s), (1, 3))

st.reset_heartbeat(s)
check("hourly counters cleared", st.coverage(s), (0, 0))
check("lifetime blind count survives", s["resale_blind_total"], 3)
check("lifetime partial count survives", s["degraded_total"], 1)
check("lifetime denominator survives", s["resale_checks_total"], 3)
# Three polls is deliberately below MIN_RESALE_SAMPLE: a percentage from a
# handful of readings is noise, and reporting one after a fresh start turned a
# single slow panel into a FAIL for a watcher that was working perfectly.
check("but three polls is too few to draw a verdict from",
      st.resale_visibility(s)[0], "unknown")

for _ in range(st.MIN_RESALE_SAMPLE):
    poll(s, UNKNOWN)
check("once there is a real sample, it reports", st.resale_visibility(s)[0], "bad")

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
