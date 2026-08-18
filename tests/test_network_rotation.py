"""Check the "switch the MacBook to your other network" advice.

David alternates the watcher between his home Wi-Fi and his phone hotspot by
hand, so that neither connection accumulates enough request volume to get
rate-limited — and so that if one does get flagged, the other still works for
actually buying a ticket.

The watcher detects the public IP rather than asking, so switching networks
is the only thing he has to do. These checks pin that: it notices the change,
resets its counters, labels the two connections correctly, and does not nag
every single hour once it has asked.

Run with:  .venv/bin/python tests/test_network_rotation.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402

failures = []
HOME = "86.44.208.194"
HOTSPOT = "31.187.77.9"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def fresh():
    config.HOME_NETWORK_IP = None
    return dict(st._defaults())


print("\nNoticing which connection is in use")

s = fresh()
# note_network() reports what KIND of change it was, not merely that there was
# one: "" for none, "readdressed" for the same connection on a new address,
# "switched" for a different connection. The distinction used to be guessed at
# by comparing labels, and got it wrong in both directions.
check("first sighting is not a 'change'", st.note_network(s, HOME), "")
check("it is recorded as current", s["current_ip"], HOME)
check("and counted", s["searches_on_current_ip"], 1)

st.note_network(s, HOME)
check("same connection keeps counting", s["searches_on_current_ip"], 2)

check("switching is detected", st.note_network(s, HOTSPOT), "switched")
check("counters reset on the new connection", s["searches_on_current_ip"], 1)
check("current connection updated", s["current_ip"], HOTSPOT)

check("a failed IP lookup changes nothing", st.note_network(s, None), "")
check("...and does not count as a search", s["searches_on_current_ip"], 1)

print("\nLabelling the two connections")

check("the first one seen is home", st.network_label(s, HOME), "home Wi-Fi")
check("the other is the hotspot", st.network_label(s, HOTSPOT), config.HOTSPOT_LABEL)
check("the other-name flips correctly", st.other_network_label(s), "home Wi-Fi")

config.HOME_NETWORK_IP = HOTSPOT
check("EP_HOME_IP overrides the guess", st.network_label(s, HOTSPOT), "home Wi-Fi")
config.HOME_NETWORK_IP = None

print("\nWhen to ask for a switch")

s = fresh()
st.note_network(s, HOME)
switch, _ = st.should_rotate_network(s)
check("not immediately", switch, False)

s["searches_on_current_ip"] = config.NETWORK_ROTATE_SEARCHES
switch, reason = st.should_rotate_network(s)
check("once the search cap is hit", switch, True)
check_true("and says why", "searches" in reason)

s = fresh()
st.note_network(s, HOME)
s["current_ip_since"] = (st.utc_now() - timedelta(hours=config.NETWORK_ROTATE_HOURS + 0.1)).isoformat()
switch, reason = st.should_rotate_network(s)
check("or once enough time has passed", switch, True)
check_true("and says that too", "on this connection" in reason)

print("\nIt must not nag every hour")

st.mark_rotation_asked(s)
switch, _ = st.should_rotate_network(s)
check("quiet straight after asking", switch, False)

s["rotation_asked_at"] = (st.utc_now() - timedelta(hours=config.NETWORK_ROTATE_HOURS + 0.1)).isoformat()
switch, _ = st.should_rotate_network(s)
check("asks again after the window", switch, True)

print("\nSwitching networks clears the nag")

s = fresh()
st.note_network(s, HOME)
s["searches_on_current_ip"] = config.NETWORK_ROTATE_SEARCHES
st.mark_rotation_asked(s)
st.note_network(s, HOTSPOT)
check("the ask is reset by an actual switch", s["rotation_asked_at"], None)
switch, _ = st.should_rotate_network(s)
check("and it stops asking", switch, False)

print("\nThe instruction has to be followable")

s = fresh()
st.note_network(s, HOME)
s["searches_on_current_ip"] = config.NETWORK_ROTATE_SEARCHES
should, headline, instruction = st.network_status(s)
check("it asks", should, True)
check_true("names the connection it is on", "home Wi-Fi" in headline)
check_true("shows the IP", HOME in headline)
check_true("says where to move to", config.HOTSPOT_LABEL in instruction)
check_true("says how, in menu-bar terms", "Wi-Fi icon" in instruction)
check_true("mentions Personal Hotspot", "Personal Hotspot" in instruction)
check_true("reassures there is nothing else to do", "Nothing else to do" in instruction)

st.note_network(s, HOTSPOT)
s["searches_on_current_ip"] = config.NETWORK_ROTATE_SEARCHES
should, headline, instruction = st.network_status(s)
check("it asks for the return trip too", should, True)
check_true("back to home Wi-Fi", "home Wi-Fi" in instruction)
check_true("and says to turn the hotspot off", "hotspot back off" in instruction)

print("\nBlocks are attributed to the connection they happened on")

s = fresh()
st.note_network(s, HOME)
# Spaced on purpose: record_block() collapses anything inside two minutes
# into a single episode, because one cycle polls every page within seconds
# and a lone 403 used to be written once per page.
st.record_block(s, when=st.utc_now() - timedelta(minutes=30))
st.record_block(s, when=st.utc_now() - timedelta(minutes=20))
st.note_network(s, HOTSPOT)
st.record_block(s, when=st.utc_now() - timedelta(minutes=10))
check("home blocks counted against home", s["networks"][HOME]["blocks"], 2)
check("hotspot blocks against the hotspot", s["networks"][HOTSPOT]["blocks"], 1)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
