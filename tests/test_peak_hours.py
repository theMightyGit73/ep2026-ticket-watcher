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
    # Hours inside the night window. 06:00 left it on 2026-08-21 —
    # see config.NIGHT_END_HOUR — so 05:00 is the late boundary now.
    for hour in (0, 3, 5):
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
# The two weekend pages are peers as of 2026-08-21. The standard page used to
# be required to outpace the instalment one; that ranking came from supply and
# was reversed by how long listings actually survive on each. See
# tests/test_multi_event.py.
check("the two weekend pages draw from the same window",
      BUSY.gap_range(at(14)), QUIET.gap_range(at(14)))

print("\nThe loop must tick fast enough to honour the fastest window")
# Ticking at the ordinary range's floor would make the peak window's shorter
# draws unreachable — the page would not be looked at until the next tick.
# The tick is the RESOLUTION at which a due page is noticed, not a request
# rate — a tick with nothing due opens no page. Measured live on 2026-08-19,
# a 300s tick against a 300-540s target delivered gaps of 5, 11, 6, 7, 11, 8,
# 12 minutes: a configured mean of 7 arriving as nearly 9, because a page
# coming due at 387s waits for the tick at 600s.
check_true("the tick is fine enough to honour the shortest gap",
           config.poll_interval() <= min(e.fastest_gap_seconds for e in config.EVENTS))
check_true("and fine enough that quantising cannot widen a gap much",
           config.poll_interval() * 1.25 <= BUSY.gap_range(at(14))[0] * 0.35)
check_true("but never absurdly fast", config.poll_interval() >= 30)

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

print("\nThe standard page has the budget the Early Entry Pass was spending")
# The parity of 2026-08-19 — the pass searched exactly as hard as the ticket —
# is gone, and with it the slowdown it forced on the standard page. What is
# pinned here is the state that replaced it: the pass off entirely, and the
# standard page back on the 3-6 minute clock it had before it started paying
# for the pass.
EARLY = next(e for e in config.EVENTS if e.slug == "early-entry")
STANDARD = next(e for e in config.EVENTS if e.slug == "weekend-camping")
INSTALMENT = next(e for e in config.EVENTS
                  if e.slug == "weekend-camping-instalment")

check("the pass is not searched at all", EARLY.searchable(), False)
check("nor secured", EARLY.secure, False)
check_true("while both weekend pages are searched and secured",
           all(e.searchable() and e.secure for e in config.EVENTS
               if e.slug != "early-entry"))
check("the standard page is on the shared weekend peak clock",
      STANDARD.gap_range(at(14)),
      (config.STANDARD_PEAK_MIN_SECONDS, config.STANDARD_PEAK_MAX_SECONDS))
# The pass keeps a range for the day it is switched back on, and it is its own
# rather than the standard page's. Inheriting the standard page's new speed
# would put peak load at 28/hour the moment the switch was thrown — see
# tests/test_early_entry_switch.py, which asserts the sum still fits.
check_true("the pass's dormant clock is slower than the ticket's",
           EARLY.gap_range(at(14))[0] > STANDARD.gap_range(at(14))[0])

# Every page must be reachable by the loop, or one is watched in name only.
for event in config.EVENTS:
    check_true(f"[{event.slug}] the tick can honour its fastest draw",
               config.poll_interval() <= event.fastest_gap_seconds)
    check_true(f"[{event.slug}] off-peak is slower than peak, not faster",
               event.gap_range(at(22))[0] >= event.gap_range(at(14))[0])
    check_true(f"[{event.slug}] has an id the resale endpoint can be keyed on",
               event.tm_event_id or "/event/" in event.url)

# What the current setting costs, stated so it cannot be quietly given back.
#
# This assertion has now been written twice in opposite directions, which is
# the useful part. It first pinned the standard page at a 270s mean, on the
# grounds that slowing it to pay for parity with the instalment page spent the
# ticket's budget on something else.
#
# On 2026-08-21 David asked for the two weekend pages to be searched alike,
# and the measurements supported it: a listing survives a median 2.1 minutes
# on the standard page and 21.8 on the instalment one, so the page being
# protected was the one we lose regardless and the page being starved was the
# one we can win. Paying for that parity costs the standard page its 270s mean.
#
# The bound below is what keeps it honest. Both weekend pages at this cadence,
# PLUS the Early Entry Pass when it is switched back on, must still fit under
# the 20/hour that drew a real block — which is the only number here that was
# ever measured rather than chosen.
check("the two weekend pages run the same clock",
      STANDARD.poll_seconds, INSTALMENT.poll_seconds)
check_true("and the ceiling still holds", config.peak_searches_per_hour() < 20)
# The tick has to keep up with the faster page or the gain is quantised away
# — a page due at 180s that is not noticed for another 45s is not running at
# 3-6 minutes however the config reads.
check_true("and the tick is fine enough to deliver it",
           config.poll_interval() <= STANDARD.gap_range(at(14))[0] / 3.0)


print("\nA page with no peak configured is unaffected")
plain = config.Event(slug="x", name="X", url="https://e/event/ABC",
                     poll_min_seconds=300, poll_max_seconds=600)
check("same range at peak and off-peak",
      plain.gap_range(at(14)), plain.gap_range(at(22)))
check("and its fastest gap is just its floor", plain.fastest_gap_seconds, 300)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
