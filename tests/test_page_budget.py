"""Attention goes where listings actually appear.

Both pages used to be searched on every cycle, which sounds fair and is not.
Of the nine resale sightings recorded between 13 and 18 August 2026, EIGHT
were on the standard Weekend Camping page and one was on the instalment plan —
so an even split spent half the request budget for an eighth of the return.

The gain comes from how short these listings are. Seven of the eight distinct
sightings were visible on exactly one poll and gone by the next, which is the
signature of a lifetime at or below the poll interval. Fitting that ratio to an
exponential gives a mean life of about 4.6 minutes and a detection chance near
40% at a 10-minute cycle; at 6 minutes it is about 56%.

So each page carries its own interval and is searched when it comes due. The
crucial property is that this costs nothing: 10 searches an hour on the busy
page plus 2 on the quiet one is the same 12 an hour the even split was already
spending. Spending more is a separate decision with a separate risk, and this
test exists to stop the two being confused.

Run with:  .venv/bin/python tests/test_page_budget.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


A, B = config.EVENTS[0], config.EVENTS[-1]


def fresh():
    return dict(st._defaults())


def aged(state, event, minutes):
    st.event_state(state, event.slug)["last_polled_at"] = (
        st.utc_now() - timedelta(minutes=minutes)
    ).isoformat()


print("\nThe budget is weighted, and it is the same budget")

check("total volume reflects the 3-6 minute standard page",
      round(config.searches_per_hour()), 15)
check_true("the busy page is searched more often", A.poll_seconds < B.poll_seconds)
check("the tick follows the busiest page's floor",
      config.poll_interval(), A.poll_min_seconds)
check_true("and the busy page carries most of the volume",
           A.searches_per_hour > B.searches_per_hour * 3)

print("\nA page is searched when it comes due, and not before")

s = fresh()
check("a page never searched is due", st.event_due(s, A), True)
# The gap is pinned rather than left to the draw. Measuring "slightly early"
# against A.poll_seconds — the MEAN of the range — made this test flaky the
# moment gaps became ranges on 2026-08-19: whether 0.8 of the mean cleared the
# tolerance depended on where that poll's draw had landed. The behaviour under
# test is about the gap actually in force, so the test states it.
GAP = 300
st.note_event_polled(s, A.slug, GAP)
check("...and not again immediately", st.event_due(s, A), False)

aged(s, A, GAP / 60 + 1)
check("due once its gap has passed", st.event_due(s, A), True)

# The loop's sleep is jittered by ±25%, so a tick landing a few seconds short
# must not skip the page and silently double its real interval.
aged(s, A, GAP / 60 * 0.8)
check("a tick landing slightly early still counts", st.event_due(s, A), True)
aged(s, A, GAP / 60 * 0.5)
check("but a tick landing far early does not", st.event_due(s, A), False)

print("\nThe quiet page is not forgotten, only slowed")

s = fresh()
st.note_event_polled(s, A.slug)
st.note_event_polled(s, B.slug)
aged(s, A, 7)
aged(s, B, 7)
due = [e.slug for e in st.due_events(s, config.EVENTS)]
check("after 7 minutes only the busy page is due", due, [A.slug])

aged(s, B, 31)
due = [e.slug for e in st.due_events(s, config.EVENTS)]
check_true("after 31 the quiet one is too", B.slug in due)

print("\nAn early tick must do nothing, and that is deliberate")
# Reversed on 2026-08-19. This used to force the longest-waiting page whenever
# nothing was strictly due, so that a tick was never a silent no-op. That was
# right while the tick equalled the busiest page's interval — something was
# always due. With gaps drawn from a range the loop must tick faster than the
# shortest possible gap, so most ticks legitimately find nothing due; forcing
# a poll on those would search the page at the tick rate and throw the whole
# range away.

s = fresh()
st.note_event_polled(s, A.slug, A.poll_max_seconds)
st.note_event_polled(s, B.slug, B.poll_max_seconds)
aged(s, A, 1)
aged(s, B, 2)
check("nothing due means nothing searched", st.due_events(s, config.EVENTS), [])

# The protection the old rule provided is kept for the case it was really for:
# a timestamp that has gone bad, or a clock that jumped, must not park a page
# for ever.
aged(s, A, (A.poll_max_seconds * 3) / 60)
due = [e.slug for e in st.due_events(s, config.EVENTS)]
check_true("a page far past its longest gap is searched anyway", A.slug in due)

check("no events configured means nothing to do", st.due_events(s, []), [])

print("\nThe drawn gap is honoured, not re-rolled")
# The trap this design exists to avoid: re-drawing on every tick collapses the
# range to its floor, because the page goes due the first time a draw lands
# low. The gap is drawn once, stored, and compared against.

s = fresh()
st.note_event_polled(s, A.slug, A.poll_max_seconds)
check("a page that drew its longest gap waits it out",
      st.event_gap_seconds(s, A), A.poll_max_seconds)
aged(s, A, A.poll_min_seconds / 60)
check("and is not due at the range floor", st.event_due(s, A), False)
check_true("the stored gap survives repeated asking",
           all(st.event_gap_seconds(s, A) == A.poll_max_seconds for _ in range(20)))

# Draws stay inside the configured range, and actually vary.
draws = {A.next_gap() for _ in range(200)}
check_true("every draw is within the range",
           all(A.poll_min_seconds <= d <= A.poll_max_seconds for d in draws))
check_true("and the range is genuinely used", len(draws) > 20)
check_true("a page with no range drawn is stable", B.next_gap() >= B.poll_min_seconds)

print("\nThe hourly report says how old each reading is")
# The pages are read at different rates now, so two statuses side by side can
# be minutes and half an hour old. Without the age, the older one reads as
# being exactly as fresh as the newer.

s = fresh()
st.note_event_polled(s, A.slug)
aged(s, A, 4)
aged(s, B, 26)
rows = st.event_summaries(s)
check("every page is reported", len(rows), len(config.EVENTS))
check("with its age", round(rows[0][4]), 4)
check("...for each of them", round(rows[-1][4]), 26)

print("\nA second watcher looks more often without talking more often")
# Two watchers on different connections sample the page twice as often while
# each keeps its own request rate. The second must not also double the routine
# post — two copies of "no luck yet" every hour is how the alert that matters
# ends up in a stream nobody reads.

import importlib  # noqa: E402
import os  # noqa: E402

was = dict(os.environ)
try:
    os.environ["EP_ROLE"] = "secondary"
    os.environ.pop("EP_HEARTBEAT_HOURS", None)
    secondary = importlib.reload(config)
    check("a secondary knows what it is", secondary.IS_SECONDARY, True)
    check_true("and reports on a much slower clock",
               secondary.HEARTBEAT_HOURS >= 6)

    os.environ["EP_HEARTBEAT_HOURS"] = "3"
    secondary = importlib.reload(config)
    check("an explicit setting still wins", secondary.HEARTBEAT_HOURS, 3.0)

    os.environ.clear(); os.environ.update(was)
    os.environ.pop("EP_ROLE", None)
    os.environ.pop("EP_HEARTBEAT_HOURS", None)
    primary = importlib.reload(config)
    check("a primary is the default", primary.IS_SECONDARY, False)
    check("and reports hourly", primary.HEARTBEAT_HOURS, 1.0)

    os.environ["EP_POLL_PHASE"] = "0.5"
    phased = importlib.reload(config)
    check("the phase offset is read", phased.POLL_PHASE, 0.5)
    os.environ["EP_POLL_PHASE"] = "9"
    check_true("and clamped to one tick", importlib.reload(config).POLL_PHASE <= 1.0)
finally:
    os.environ.clear(); os.environ.update(was)
    importlib.reload(config)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
