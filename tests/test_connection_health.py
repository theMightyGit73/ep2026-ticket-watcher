"""Check the connection-health assessment and the advice it produces.

This exists because of a specific incident. On 2026-08-13 the watcher polled
every three minutes, Ticketmaster rate-limited the connection, and the block
escalated to the *home* IP — stopping ordinary manual browsing. That is the
worst case: the home connection is the one needed to actually buy a ticket.

The failure was not that it got blocked; it was that nothing said so. A run
of quiet failures looks identical to a quiet Ticketmaster. These checks pin
the behaviour that fixes that: count the blocks, say plainly how bad it is,
and give instructions worth following.

Run with:  .venv/bin/python tests/test_connection_health.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def fresh():
    return dict(st._defaults())


def with_blocks(count, minutes_ago):
    """State whose block history holds `count` blocks, all `minutes_ago` old."""
    s = fresh()
    when = (st.utc_now() - timedelta(minutes=minutes_ago)).isoformat()
    s["block_history"] = [when] * count
    return s


print("\nCounting blocks in a window")

s = fresh()
check("a clean state has none", st.recent_blocks(s, 24), 0)

s = with_blocks(3, minutes_ago=30)
check("blocks inside the window count", st.recent_blocks(s, 1), 3)
check("...and inside a wider one", st.recent_blocks(s, 24), 3)

s = with_blocks(3, minutes_ago=120)
check("blocks outside the hour do not", st.recent_blocks(s, 1), 0)
check("...but still count for the day", st.recent_blocks(s, 24), 3)

print("\nRecording prunes ancient history")

s = fresh()
s["block_history"] = [(st.utc_now() - timedelta(days=9)).isoformat()] * 5
st.record_block(s)
check("week-old entries are dropped", len(s["block_history"]), 1)

print("\nSeverity")

severity, headline, action = st.connection_health(fresh())
check("clean connection is ok", severity, "ok")
check_true("and says so", "healthy" in headline)
check_true("with nothing to do", "Nothing to do" in action)

severity, headline, action = st.connection_health(with_blocks(2, minutes_ago=180))
check("a few old blocks is only 'watch'", severity, "watch")
check_true("and explicitly says recovered", "recovered" in headline)

severity, headline, action = st.connection_health(with_blocks(1, minutes_ago=10))
check("one recent block is 'watch', not panic", severity, "watch")
check_true("suggests mobile data for browsing now", "mobile data" in action)

severity, headline, action = st.connection_health(with_blocks(8, minutes_ago=10))
check("sustained blocking is 'blocked'", severity, "blocked")

print("\nThe advice given while blocked must be actionable")
check_true("tells him to stop the watcher", "Stop the watcher" in action)
check_true("gives the actual unload command", "launchctl unload" in action)
check_true("points at mobile data", "mobile data" in action)
check_true("mentions signing in", "Sign in" in action)
check_true("says it decays on its own", "decay" in action)
check_true("says to slow down before restarting", "EP_POLL_SECONDS" in action)

print("\nA blocked connection must never look like 'no tickets'")
from ep_watcher.model import Reading, UNKNOWN  # noqa: E402

blocked = Reading(source="browser", blocked=True, failed=True)
check("blocked readings are failures", blocked.failed, True)
check("and carry no availability claim", blocked.any_good, False)
check("primary stays unknown", blocked.primary, UNKNOWN)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
