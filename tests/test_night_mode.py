"""Check the overnight slowdown.

The watcher's value is a headstart, and a headstart is worth nothing at 3am:
you cannot act on a resale listing while asleep, and those listings last
about five minutes. So the overnight hours buy almost no coverage while
quietly accumulating request volume on whichever connection is in use, with
nobody awake to notice a block.

Slowing overnight cuts that load and leaves the connection fresh for the
morning, which is when a headstart actually counts.

Run with:  .venv/bin/python tests/test_night_mode.py
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


def at(hour):
    return datetime(2026, 8, 14, hour, 30)


print(f"\nNight window is {config.NIGHT_START_HOUR:02d}:00-{config.NIGHT_END_HOUR:02d}:00 local")

check("early hours are night", config.is_night(at(2)), True)
check("just before the end is night", config.is_night(at(6)), True)
check("the end hour itself is daytime", config.is_night(at(7)), False)
check("mid-morning is daytime", config.is_night(at(10)), False)
check("afternoon is daytime", config.is_night(at(15)), False)
check("late evening is daytime", config.is_night(at(23)), False)
check("midnight is night", config.is_night(at(0)), True)

print("\nA window that wraps past midnight")

saved = (config.NIGHT_START_HOUR, config.NIGHT_END_HOUR)
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 23, 7
check("23:30 is night", config.is_night(at(23)), True)
check("02:30 is night", config.is_night(at(2)), True)
check("12:30 is not", config.is_night(at(12)), False)
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved

print("\nIntervals")

seconds, night = config.poll_interval_now(600)
check("returns a sane pair", isinstance(seconds, int) and seconds >= 600, True)

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 24
seconds, night = config.poll_interval_now(600)
check("night slows things down", night, True)
check("to the night interval", seconds, config.NIGHT_POLL_SECONDS)

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 0
seconds, night = config.poll_interval_now(600)
check("an empty window means never night", night, False)
check("so the daytime interval stands", seconds, 600)

print("\nNight must never make it poll FASTER")

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 24
seconds, _ = config.poll_interval_now(3600)   # daytime already slower
check("a slower daytime setting wins", seconds, 3600)
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved

print("\nThe search timeout is longer overnight")
# Every observed non-resolving search — five across 2026-08-15, -16 and -17 —
# fell between 22:08 and 01:00, against hundreds of daytime polls with none.
# Each costs a resale-blind poll on both pages. Waiting longer is paid only by
# searches that were going to fail: a healthy one returns as soon as its
# marker appears, so the ceiling is never reached on a good poll.

from datetime import datetime  # noqa: E402

saved = config.NIGHT_START_HOUR, config.NIGHT_END_HOUR
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 7
at = datetime(2026, 8, 17, 12, 0)

check("by day it is the ordinary timeout",
      config.search_timeout(at.replace(hour=12)), config.SEARCH_TIMEOUT_SECONDS)
check("at 01:00, when they actually happened, it is longer",
      config.search_timeout(at.replace(hour=1)), config.NIGHT_SEARCH_TIMEOUT_SECONDS)
# Night must never be MORE impatient than day — that is the invariant, and it
# survives the two values being equal. They are equal now: on 2026-08-18 two
# searches timed out at 11:14 and 11:17, in broad daylight, after a power cut
# moved the watcher onto a mobile connection. The page is not only slow at
# night, it is slow over a slow link, so the daytime ceiling was raised to
# match. Waiting longer is paid only by searches that were going to fail.
check("night is never less patient than day",
      config.NIGHT_SEARCH_TIMEOUT_SECONDS >= config.SEARCH_TIMEOUT_SECONDS, True)
check("06:59 is still night", config.search_timeout(at.replace(hour=6)),
      config.NIGHT_SEARCH_TIMEOUT_SECONDS)
check("07:00 is not", config.search_timeout(at.replace(hour=7)),
      config.SEARCH_TIMEOUT_SECONDS)

# It must not stretch a cycle far enough to look like a hang. Worst case is
# every page timing out, and the watchdog now measures against next_poll_due
# plus a 15-minute grace rather than a flat limit.
worst = config.NIGHT_SEARCH_TIMEOUT_SECONDS * len(config.EVENTS)
check("even an all-timeout night cycle fits inside the watchdog grace",
      worst < 15 * 60, True)

print("\nThe two night settings are independent")
# The window says when Ticketmaster is slow; NIGHT_POLL_SECONDS says how often
# we choose to ask. Turning the slowdown off must not also remove the extra
# patience, or disabling one knob silently changes the other.

saved_night = config.NIGHT_POLL_SECONDS
config.NIGHT_POLL_SECONDS = 0
check("with the overnight slowdown off, the interval is unchanged",
      config.poll_interval_now(600)[0], 600)
check("...but the search is still given longer at 01:00",
      config.search_timeout(at.replace(hour=1)), config.NIGHT_SEARCH_TIMEOUT_SECONDS)
config.NIGHT_POLL_SECONDS = saved_night
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved

print("\nIt can be turned off entirely")

saved_night = config.NIGHT_POLL_SECONDS
config.NIGHT_POLL_SECONDS = 0
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 24
seconds, night = config.poll_interval_now(600)
check("disabled means no night mode", night, False)
check("and the normal interval throughout", seconds, 600)
config.NIGHT_POLL_SECONDS = saved_night
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
