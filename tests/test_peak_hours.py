"""Searching hardest when tickets are actually being listed.

David's idea, on 2026-08-19: people resell in the evening, so look more often
then. The eight sightings recorded by that date agree with the instinct and
disagree with the hours — all eight fell between 08:00 and 20:00 local, none
overnight, and his suggested 15:00-22:00 would have covered only three:

    08:49  10:09  11:35  14:14  14:32  17:02  18:33  19:57   (local)

Measured as sightings per hour of clock, 10:00-20:00 is the best window
available at 2.1x enrichment, against 1.29x for 15:00-22:00. Eight is a small
sample and the window is configurable for exactly that reason.

The rule these checks exist to protect: this is the SAME budget spent
differently. Off-peak slows by as much as peak speeds up, so a day costs no
more than the flat cadence it replaced, and the busiest hour stays under the
~20 searches/hour that got the home connection blocked in development.

Run with:  .venv/bin/python tests/test_peak_hours.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def at(hour, minute=30):
    return datetime(2026, 8, 19, hour, minute)


BUSY, QUIET = config.EVENTS[0], config.EVENTS[1]

print("\nThe window covers the hours listings actually appeared in")
SIGHTINGS = [8, 10, 11, 14, 14, 17, 18, 19]     # local hours, observed
covered = [h for h in SIGHTINGS if config.is_peak(at(h))]
check_true("most observed sightings fall inside the peak window",
           len(covered) >= 6)
check("and the window is the one the evidence chose",
      (config.PEAK_START_HOUR, config.PEAK_END_HOUR), (10, 20))

print("\nEach hour lands in exactly one window")
for hour in range(24):
    night, peak = config.is_night(at(hour)), config.is_peak(at(hour))
    check(f"{hour:02d}:00 is not both night and peak", night and peak, False)
check_true("03:00 is night", config.is_night(at(3)))
check_true("14:00 is peak", config.is_peak(at(14)))
check("08:00 is neither", (config.is_night(at(8)), config.is_peak(at(8))), (False, False))
check("21:00 is neither", (config.is_night(at(21)), config.is_peak(at(21))), (False, False))
# The boundaries, which is where an off-by-one would hide.
check_true("10:00 is the first peak hour", config.is_peak(at(10, 0)))
check("09:59 is not yet peak", config.is_peak(at(9, 59)), False)
check_true("19:59 is still peak", config.is_peak(at(19, 59)))
check("20:00 is past it", config.is_peak(at(20, 0)), False)

print("\nNight always wins, so a peak window can never undo the slowdown")
# If the two were ever configured to overlap, the overnight quiet must hold —
# it exists to keep the watcher off the network while nobody is listing.
real_start, real_end = config.PEAK_START_HOUR, config.PEAK_END_HOUR
try:
    config.PEAK_START_HOUR, config.PEAK_END_HOUR = 0, 24
    for hour in (0, 3, 6):
        check(f"{hour:02d}:00 stays off-peak despite an all-day peak window",
              config.is_peak(at(hour)), False)
    check_true("but midday is peak", config.is_peak(at(12)))
finally:
    config.PEAK_START_HOUR, config.PEAK_END_HOUR = real_start, real_end

print("\nThe busy page really is searched harder at peak")
peak_lo, peak_hi = BUSY.gap_range(at(14))
off_lo, off_hi = BUSY.gap_range(at(22))
check_true("peak gaps are shorter than off-peak", peak_hi <= off_lo)
check_true("and every draw respects the window it was made in",
           all(peak_lo <= BUSY.next_gap(at(14)) <= peak_hi for _ in range(200)))
check_true("off-peak too",
           all(off_lo <= BUSY.next_gap(at(22)) <= off_hi for _ in range(200)))
check_true("the quiet page is weighted the same way",
           QUIET.gap_range(at(14))[1] <= QUIET.gap_range(at(22))[0])
# The busy page must still outpace the quiet one within any window.
check_true("and the busy page is always the faster of the two",
           BUSY.gap_range(at(14))[1] < QUIET.gap_range(at(14))[0])

print("\nThe loop must tick fast enough to honour the fastest window")
# Ticking at the ordinary range's floor would make the peak window's shorter
# draws unreachable — the page would not be looked at until the next tick.
check("the tick follows the fastest gap any page can draw",
      config.poll_interval(), min(e.fastest_gap_seconds for e in config.EVENTS))
check_true("which is at least as fast as the peak floor",
           config.poll_interval() <= BUSY.gap_range(at(14))[0])

print("\nSpending the same budget, not more")
check_true("the busiest hour stays under the ~20/hour that drew a block",
           config.peak_searches_per_hour() < 20)
# Three pages since the Early Entry Pass was added, so the day is dearer than
# the two-page flat cadence it replaced. The binding constraint is the
# instantaneous rate above, not this; this exists so a fourth page cannot be
# added without somebody noticing what it costs.
check_true("a day stays within reach of the flat cadence it replaced",
           config.searches_per_day() <= 300)
check_true("peak hours are busier than off-peak",
           config.searches_per_hour_at(14) > config.searches_per_hour_at(22))
check_true("and off-peak busier than night",
           config.searches_per_hour_at(22) > config.searches_per_hour_at(3))
# Concentration is the point: the peak window should carry a clearly larger
# share of the day's searches than its share of the clock.
peak_hours = [h for h in range(24) if config.is_peak(at(h))]
share = sum(config.searches_per_hour_at(h) for h in peak_hours) / config.searches_per_day()
clock = len(peak_hours) / 24.0
check_true(f"peak carries {share:.0%} of the searches in {clock:.0%} of the day",
           share > clock * 1.4)

print("\nA page with no peak configured is unaffected")
plain = config.Event(slug="x", name="X", url="https://e/event/ABC",
                     poll_min_seconds=300, poll_max_seconds=600)
check("same range at peak and off-peak",
      plain.gap_range(at(14)), plain.gap_range(at(22)))
check("and its fastest gap is just its floor", plain.fastest_gap_seconds, 300)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
