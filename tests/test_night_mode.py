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
