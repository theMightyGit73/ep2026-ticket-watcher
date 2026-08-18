"""Step around the wall rather than walking into it.

Ticketmaster's bot-check cookies age out. Across 28 blocks in six days, every
single one was cleared by a fresh browser profile on the first attempt, and the
exponential backoff behind that reset was never once reached — so the wall is
carried in the profile, not in the IP. The watcher's own reset_profile() had
already recorded the other half of the evidence: after a block, moving to a
completely different network did NOT clear it, while a fresh profile on the
same network worked first try.

Waiting for the wall costs two resale-blind readings and a wasted cycle each
time, four to ten times a day. Refreshing early costs one cold page load, spent
in a sleep window. So the identity is rebuilt on a timer.

Two things this must not do: reset a profile whose age is unknown (that would
throw away a good session on every restart), and reset at all when the timer is
switched off.

Run with:  .venv/bin/python tests/test_profile_refresh.py
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


def aged(minutes):
    s = dict(st._defaults())
    s["profile_reset_at"] = (st.utc_now() - timedelta(minutes=minutes)).isoformat()
    return s


print("\nThe identity clock")

s = dict(st._defaults())
check("a brand new state has no age", st.profile_age_minutes(s), None)
check("and is never called stale on no evidence", st.profile_is_stale(s), False)

st.note_profile_reset(s)
check("recording a reset starts the clock", round(st.profile_age_minutes(s)), 0)
check("a fresh profile is not stale", st.profile_is_stale(s), False)

limit = config.PROFILE_MAX_AGE_MINUTES
check("just under the limit is left alone", st.profile_is_stale(aged(limit - 5)), False)
check("past the limit is due a refresh", st.profile_is_stale(aged(limit + 1)), True)

print("\nThe limit sits inside the observed range")
# 64 minutes was the shortest gap ever seen between two blocks; the common
# daytime cluster is around two hours. A limit above the floor still catches
# most episodes, but one above the cluster would catch almost none.
check("under the two-hour cluster", limit < 120, True)
check("and not so eager it churns the profile", limit >= 45, True)

print("\nIt can be switched off")

was = config.PROFILE_MAX_AGE_MINUTES
config.PROFILE_MAX_AGE_MINUTES = 0
try:
    check("zero disables the refresh entirely", st.profile_is_stale(aged(10_000)), False)
finally:
    config.PROFILE_MAX_AGE_MINUTES = was

print("\nOne 403 is one block, however many pages saw it")
# handle() runs per watched page, so a single wall used to be written once per
# page. connection_health() reads those counts against thresholds set when one
# page was watched: a lone episode already graded "watch", and a third page
# would have graded it "blocked" — whose advice is to stop the watcher.

s = dict(st._defaults())
st.note_network(s, "86.44.208.194")
for _ in config.EVENTS:
    st.record_block(s)
check("both pages, one episode", st.recent_blocks(s, 1), 1)
check("and one against the connection", s["networks"]["86.44.208.194"]["blocks"], 1)

st.record_block(s, when=st.utc_now() + timedelta(minutes=5))
check("a genuinely separate wall still counts", st.recent_blocks(s, 1), 2)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
