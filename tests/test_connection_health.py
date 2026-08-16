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

print("\nBlocks belong to a connection, not just to a moment")
# The failure this fixes: block history was time-only, so after switching from
# a flagged home Wi-Fi to a clean hotspot the emails kept reporting the block
# for another 24 hours — and reported it against the NEW connection. That
# actively contradicts the advice to switch, which is the one instruction the
# whole rotation scheme depends on him following.

HOME, HOTSPOT = "86.44.208.194", "212.129.87.241"


def on_connection(ip, blocks=(), minutes_ago=10):
    """State sitting on `ip`, with `blocks` recorded against named IPs."""
    s = fresh()
    s["networks"] = {
        HOME: {"first_seen": "2026-08-16T11:00:00+00:00", "searches": 80, "blocks": 0},
        HOTSPOT: {"first_seen": "2026-08-16T18:00:00+00:00", "searches": 2, "blocks": 0},
    }
    s["current_ip"] = ip
    when = (st.utc_now() - timedelta(minutes=minutes_ago)).isoformat()
    s["block_history"] = [{"at": when, "ip": at_ip} for at_ip in blocks]
    return s


import ep_watcher.config as cfg  # noqa: E402

cfg.HOME_NETWORK_IP = HOME

s = on_connection(HOME, blocks=[HOME] * 4)
check("blocks on this connection count", st.recent_blocks(s, 1, ip=HOME), 4)
check("but not against the other one", st.recent_blocks(s, 1, ip=HOTSPOT), 0)
check("and the unfiltered count still sees them all", st.recent_blocks(s, 1), 4)

severity, headline, action = st.connection_health(s)
check("the connection in use is called blocked", severity, "blocked")
check_true("and is named", "home Wi-Fi" in headline)

print("\nSwitching away from a flagged connection must clear the verdict")

s = on_connection(HOTSPOT, blocks=[HOME] * 4)
severity, headline, action = st.connection_health(s)
check("the fresh connection is not blamed", severity, "ok")
check_true("it is reported healthy", "healthy" in headline)
check_true("while still naming the burnt one", "home Wi-Fi" in headline)
check_true("...and saying it is the one in trouble", "in trouble" in headline)
check_true("...with the count", "4 block" in headline)

print("\nRecording attaches the current connection")

s = on_connection(HOME)
st.record_block(s)
check("the entry knows where it happened", s["block_history"][-1]["ip"], HOME)
check("and the per-connection tally advances", s["networks"][HOME]["blocks"], 1)
check("leaving the other alone", s["networks"][HOTSPOT]["blocks"], 0)

print("\nHistory written before blocks carried an IP is not lost")
# Old entries are bare strings. They count against whichever connection is
# asked about: over-warning about the connection in use is far cheaper than
# going quiet about a real block.

s = on_connection(HOME)
s["block_history"] = [(st.utc_now() - timedelta(minutes=5)).isoformat()] * 3
check("legacy entries still count", st.recent_blocks(s, 1, ip=HOME), 3)
check("and are not attributed elsewhere", st.blocks_elsewhere(s, 24), [])
severity, _, _ = st.connection_health(s)
check("so an old block still raises a verdict", severity, "blocked")

st.record_block(s)
check("a new block upgrades the whole history to the new shape",
      all(isinstance(e, dict) for e in s["block_history"]), True)
check("without dropping the old entries", len(s["block_history"]), 4)

print("\n...but an unattributed block must not be blamed on a named connection")
# Counting them against the current connection is the right call; *saying*
# they happened on it is not. Nothing recorded which connection they were,
# and a confident wrong name would point him away from the one really burnt.

s = on_connection(HOTSPOT)
s["block_history"] = [(st.utc_now() - timedelta(minutes=90)).isoformat()] * 4
_, headline, _ = st.connection_health(s)
check_true("the count is still reported", "4 block(s)" in headline)
check("but no connection is named", "hotspot" in headline, False)
check("and certainly not the wrong one", "home Wi-Fi" in headline, False)

# Once the same connection has an attributed block, naming it is earned.
s["block_history"].append({"at": st.utc_now().isoformat(), "ip": HOTSPOT})
_, headline, _ = st.connection_health(s)
check_true("a recent attributed block still counts", "in the last hour" in headline)

print("\nPruning still works on the new shape")

s = on_connection(HOME)
s["block_history"] = [
    {"at": (st.utc_now() - timedelta(days=9)).isoformat(), "ip": HOME}
] * 5
st.record_block(s)
check("week-old entries are dropped", len(s["block_history"]), 1)

cfg.HOME_NETWORK_IP = None

print("\nA blocked connection must never look like 'no tickets'")
from ep_watcher.model import Reading, UNKNOWN  # noqa: E402

blocked = Reading(source="browser", blocked=True, failed=True)
check("blocked readings are failures", blocked.failed, True)
check("and carry no availability claim", blocked.any_good, False)
check("primary stays unknown", blocked.primary, UNKNOWN)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
