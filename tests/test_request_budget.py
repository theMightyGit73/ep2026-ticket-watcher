"""The request budget must stay under the rate that actually drew a block.

This is the one failure mode that costs every ticket rather than some of
them. Polling every three minutes — roughly 20 searches an hour — got this
client answered with HTTP 403 on 2026-08-13, from the same headed Chrome that
had been getting 200 all day. A blocked watcher is not a slower watcher; it
sees nothing at all.

The reason this file exists is that the arithmetic kept being done in prose
and kept going stale. Three separate comment blocks in config.py claimed 12,
"~15.3" and "~17" searches an hour for a configuration that really spent 18.5,
each accurate when written and none updated when a page's range moved. The
README claimed 12 too. Nothing computed the number, so nothing could notice.

So the number is now computed, and this is the gate. Raising a page's cadence
is a perfectly reasonable thing to want to do — but it should have to argue
with a failing test rather than slip through in a comment nobody re-derives.

Run with:  .venv/bin/python tests/test_request_budget.py
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config  # noqa: E402
from ep_watcher import __main__ as cli  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


print("\nThe busiest hour stays under the rate that drew a 403")
peak = config.peak_searches_per_hour()
print(f"  (peak is {peak:.1f}/hour against a limit of {config.BLOCK_RATE_PER_HOUR:.0f})")
check_true(
    f"peak {peak:.1f}/hr is under the {config.BLOCK_RATE_PER_HOUR:.0f}/hr block line",
    peak <= config.BLOCK_RATE_PER_HOUR,
)
# Not merely under it. A configuration sitting at 19.9 is under the line in
# the same sense that a car doing 69 is under the limit, and the threshold is
# one observation on one day rather than a published number.
check_true(
    "and leaves at least a little headroom rather than sitting on the line",
    peak <= config.BLOCK_RATE_PER_HOUR * 0.98,
)

print("\nEvery hour of the day is under it, not just the average")
# The daily total can look comfortable while a single window is refused. A
# rate limit measures requests inside a window; it does not average.
worst_hour, worst_rate = max(
    ((h, config.searches_per_hour_at(h)) for h in range(24)), key=lambda p: p[1]
)
check_true(
    f"the worst single hour ({worst_hour:02d}:00, {worst_rate:.1f}/hr) is under the line",
    worst_rate <= config.BLOCK_RATE_PER_HOUR,
)
check("peak_searches_per_hour() agrees with the worst hour", round(peak, 3),
      round(worst_rate, 3))

print("\nOvernight really is quieter, and peak really is busier")
night = config.searches_per_hour_at(3)
day_offpeak = config.searches_per_hour_at(8)
day_peak = config.searches_per_hour_at(15)
check_true(f"night ({night:.1f}) is slower than off-peak day ({day_offpeak:.1f})",
           night < day_offpeak)
check_true(f"off-peak day ({day_offpeak:.1f}) is slower than peak ({day_peak:.1f})",
           day_offpeak < day_peak)

print("\nEach page's advertised rate matches the range it is actually polled on")
# The trap this catches is real and was hit on the day the Early Entry Pass
# was added: a page given only peak/off-peak ranges fell through to the
# default for poll_seconds, so searches_per_hour() reported 13.3 for a page
# really polled every half hour. gap_range() was right and the arithmetic was
# wrong, which is the combination nothing notices.
noon = datetime.datetime(2000, 1, 1, 12, 30)
for event in config.EVENTS:
    lo, hi = event.gap_range(noon)
    mean = (lo + hi) / 2.0
    # poll_seconds is what the budget arithmetic uses. It must describe the
    # cadence actually in force at peak, not some other window.
    check(f"{event.slug}: poll_seconds is the mean of the peak range",
          event.poll_seconds, int(mean))

print("\nThe loop tick keeps up with the fastest gap any page can draw")
# The tick is how often the loop wakes to ask "is anything due", not a
# request rate. If it is slower than the shortest gap, the drawn intervals
# are quantised upward: measured live on 2026-08-19, a 300s tick against a
# 300-540s target delivered gaps of 5, 11, 6, 7, 11, 8 and 12 minutes.
fastest = min(e.fastest_gap_seconds for e in config.EVENTS)
check_true(
    f"tick ({config.POLL_INTERVAL_SECONDS}s) is no slower than the fastest gap ({fastest}s)",
    config.POLL_INTERVAL_SECONDS <= fastest,
)

print("\nOne ticket, always — never widen the quantity sweep")
# David's standing instruction, and also the most sensitive probe available:
# "there aren't enough tickets" is an answer about the number you asked for.
check("the watcher searches for exactly one ticket", config.WANTED_QUANTITIES, [1])
check("and WANTED_QUANTITY agrees with it", config.WANTED_QUANTITY, 1)

print("\nThe budget report tells the truth and fails loudly when it is broken")
lines, over = cli.budget_report()
body = "\n".join(lines)
check("a healthy configuration does not report itself over budget", over, False)
check_true("the report states the peak rate", f"{peak:.1f}" in body)
check_true("and the limit it is measured against",
           f"{config.BLOCK_RATE_PER_HOUR:.0f}" in body)
for event in config.EVENTS:
    check_true(f"and names {event.slug}", event.slug in body)
# The exit code is what a workflow or a pre-push hook would branch on, so the
# over-budget path has to be exercised rather than assumed.
was = config.BLOCK_RATE_PER_HOUR
try:
    config.BLOCK_RATE_PER_HOUR = 1.0
    lines, over = cli.budget_report()
    check("an impossible limit is reported as over budget", over, True)
    check_true("and the report says so in words",
               "OVER BUDGET" in "\n".join(lines))
finally:
    config.BLOCK_RATE_PER_HOUR = was

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
