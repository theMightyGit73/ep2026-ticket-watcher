"""The Early Entry Pass is switched off, and switching it back on must work.

David's instruction on 2026-08-20: stop searching for the pass entirely,
because the weekend ticket is the critical thing and he does not have one yet,
so every request the watcher can spend should go to finding it. Explicitly
NOT a deletion — the day he has a real ticket, he wants the pass search back
on with one easy change, and wants the pass actually secured rather than
merely mentioned in an email.

That makes this file a test of a promise rather than of a behaviour. The
switched-off half is easy and would be caught by almost anything; the half
that matters is the one nobody will exercise until the afternoon it is
needed, in a hurry, with a ticket already bought. So the ON path is tested
here just as hard as the OFF path, and stays tested while it is unused.

The specific failure being guarded against is a half-switch: a page that is
searched again but only ever emails, because searching and holding were once
separate settings and the first got turned back on alone. That version looks
like it worked — alerts arrive, the log fills up — right up to the moment a
pass appears and nothing holds it.

Run with:  .venv/bin/python tests/test_early_entry_switch.py
"""

import importlib
import os
import sys
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


def early_of(module):
    return next(e for e in module.EVENTS if e.slug == "early-entry")


def weekend_of(module):
    return next(e for e in module.EVENTS if e.slug == "weekend-camping")


def fresh():
    """A state where nothing has ever been polled, so everything is due."""
    return dict(st._defaults())


print("\nOff by default, and off means untouched")

early = early_of(config)
check("the pass is not searchable", early.searchable(), False)
check("and is not secured either", early.secure, False)
# Not the same as expired. `expired` means finished for good and the watcher
# should stop mentioning it; this is a decision somebody can un-make, and the
# watcher should keep saying it was made.
check("but it has not expired — this is reversible", early.expired(), False)
check_true("it is listed as paused", early in config.paused_pages())
check_true("and the note names it", "Early Entry" in config.paused_note())
check_true("the note says how to turn it back on",
           "EP_EARLY_ENTRY" in config.paused_note())

# The scheduler is the thing that turns "not searchable" into "costs no
# requests". Everything else is commentary.
due = st.due_events(fresh(), config.EVENTS)
check_true("the scheduler never offers it, even when nothing has been polled",
           "early-entry" not in [e.slug for e in due])
check_true("while the weekend pages are still due",
           "weekend-camping" in [e.slug for e in due])

# The resale sweep is a second, separate way to spend a request on a page, and
# it reads its own list. It was the last caller to learn about stop dates and
# would have been the last to learn about this.
sweep = engine.ResaleSweep()
swept = [e.slug for e in config.EVENTS if e.searchable() and sweep.due(e, 0.0)]
check_true("the sweep does not ask about it either",
           "early-entry" not in swept)
check_true("but does ask about the weekend ticket",
           "weekend-camping" in swept)

# The hourly email drops the row rather than showing a frozen one. A status
# stuck at whatever it said when the switch was flipped, sitting beside two
# live ones, reads as a page that has broken.
summaries = st.event_summaries(fresh())
check_true("the hourly report omits it",
           not any("Early Entry" in row[0] for row in summaries))
check_true("and still reports the pages that are on",
           any("Weekend Camping" in row[0] for row in summaries))


print("\nOne flag turns the search AND the holding back on")

was = dict(os.environ)
try:
    os.environ["EP_EARLY_ENTRY"] = "1"
    on = importlib.reload(config)
    early = early_of(on)

    check("the pass is searched again", early.searchable(), True)
    # The half-switch this file exists to prevent. Searching without holding
    # is the state the watcher was in all morning on 2026-08-20, and it is a
    # perfectly reasonable thing to want — it is just not what "turn it back
    # on so I can get an early ticket" means.
    check("and held, not merely alerted on", early.secure, True)
    check_true("nothing is paused any more", not on.paused_pages())
    check("so there is nothing to nag about", on.paused_note(), "")

    check_true("the scheduler offers it again",
               "early-entry" in [e.slug for e in st.due_events(fresh(), on.EVENTS)])
    check_true("and the hourly report carries it again",
               any("Early Entry" in row[0] for row in st.event_summaries(fresh())))

    # Turning it on must not turn it into a threat to the thing it accompanies.
    # A pass that outranked a weekend ticket would close the browser on a real
    # ticket to go and get an add-on that is worthless without one — and the
    # case that flips this switch is precisely the case where a weekend ticket
    # has just been bought and must not be disturbed.
    check_true("it still gives way to a weekend ticket",
               early.secure_priority < weekend_of(on).secure_priority)
    check_true("and cannot preempt one",
               not (early.secure_priority > weekend_of(on).secure_priority))

    # Cadence and stop date survive the round trip. If switching off had
    # quietly reset them, turning it back on would look right and behave
    # differently — the pass would come back on some default clock rather than
    # the one that was chosen for it.
    # Its own range, not the standard page's. The pass was pinned to the
    # ticket's clock while David considered the two equally important; it
    # comes back as the secondary page it will be on the day he flips this,
    # and inheriting the ticket's new 3-6 minutes would put peak load at
    # 28/hour the moment the switch was thrown.
    check("its dormant cadence is intact",
          (early.peak_min_seconds, early.peak_max_seconds),
          (on.EARLY_ENTRY_PEAK_MIN_SECONDS, on.EARLY_ENTRY_PEAK_MAX_SECONDS))
    check_true("and is slower than the weekend ticket's",
               early.peak_min_seconds > weekend_of(on).peak_min_seconds)
    # The date itself is set from EP_EARLY_ENTRY_STOP_AFTER and is pinned
    # forward by tests/_sandbox.py so the suite does not rot once the
    # festival passes. What matters here is that switching the pass off
    # leaves its own stop date SET rather than blanked — the page keeps its
    # own end, independent of the switch.
    check_true("and its own stop date is intact", bool(early.stop_after))

    # The reason the whole arrangement is safe to flip in a hurry.
    check_true("turning it on stays under the block line",
               on.peak_searches_per_hour() < on.BLOCK_RATE_PER_HOUR)
finally:
    os.environ.clear(); os.environ.update(was)
    config = importlib.reload(config)

check("and it is off again afterwards", early_of(config).searchable(), False)


print("\nThe weekend pages are untouched by any of this")

# The entire point of switching the pass off is that the weekend ticket is
# unaffected except by getting more of the budget. A change that quietly
# altered the pages that matter would have missed it completely.
for slug in ("weekend-camping", "weekend-camping-instalment"):
    event = next(e for e in config.EVENTS if e.slug == slug)
    check_true(f"[{slug}] still searched", event.searchable())
    check_true(f"[{slug}] still held", event.secure)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
