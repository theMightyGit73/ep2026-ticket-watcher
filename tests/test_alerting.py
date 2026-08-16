"""Exercise the alert-gating logic without touching the network.

This is the part the old watcher got wrong: it latched a single "already
alerted" flag, so a permanent outage produced one email and then 44 days of
silence. These checks pin the behaviour that replaces it.

Run with:  .venv/bin/python tests/test_alerting.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def fresh():
    return dict(st._defaults())


def ev(state):
    """This event's availability history.

    Availability moved under state["events"][slug] when a second ticket page
    was added, so that a listing on one could not update the "last seen"
    values for the other and silence its alert. These tests use the default
    slug; what matters is that they read and write the same place the code
    does, rather than the old top-level keys the code no longer looks at.
    """
    return st.event_state(state, "")


print("\nAvailability alerting")

# Resale appearing from nothing must alert.
s = fresh()
r = Reading(source="t", primary=UNAVAILABLE, resale=AVAILABLE)
should, why = st.should_alert_availability(s, r)
check("resale UNAVAILABLE -> AVAILABLE alerts", should, True)

# Once recorded and alerted, an unchanged repeat must stay quiet.
st.record_success(s, r)
ev(s)["last_availability_alert"] = st.utc_now().isoformat()
should, _ = st.should_alert_availability(s, r)
check("same state repeated stays quiet", should, False)

# ...but it re-nags once the clock runs out, so one missed push isn't fatal.
ev(s)["last_availability_alert"] = (
    st.utc_now() - timedelta(hours=config.AVAILABILITY_RENAG_HOURS + 0.1)
).isoformat()
should, _ = st.should_alert_availability(s, r)
check("re-nags after the renag window", should, True)

# Primary appearing while resale was ALREADY available must still alert —
# a single flat boolean would have swallowed this.
s = fresh()
ev(s)["last_resale"], ev(s)["last_primary"] = AVAILABLE, UNAVAILABLE
ev(s)["last_availability_alert"] = st.utc_now().isoformat()
r2 = Reading(source="t", primary=AVAILABLE, resale=AVAILABLE)
should, why = st.should_alert_availability(s, r2)
check("primary appearing under available resale alerts", should, True)
print(f"        reason: {why}")

# A failed read must never look like "sold out".
s = fresh()
ev(s)["last_resale"] = AVAILABLE
r3 = Reading(source="t", primary=UNKNOWN, resale=UNKNOWN)
check("UNKNOWN is not 'good'", r3.any_good, False)

print("\nWatchdog")

s = fresh()
check("quiet below threshold", st.should_alert_watchdog(s), False)

s["consecutive_failures"] = config.WATCHDOG_FAILURE_THRESHOLD
check("fires at threshold", st.should_alert_watchdog(s), True)

s["last_watchdog_alert"] = st.utc_now().isoformat()
check("quiet immediately after alerting", st.should_alert_watchdog(s), False)

s["last_watchdog_alert"] = (
    st.utc_now() - timedelta(hours=config.WATCHDOG_RENAG_HOURS + 0.1)
).isoformat()
check("re-nags rather than latching (the old bug)", st.should_alert_watchdog(s), True)

print("\nStatus merging")
from ep_watcher.model import better_status  # noqa: E402

check("definite beats unknown", better_status(UNKNOWN, UNAVAILABLE), UNAVAILABLE)
check("available beats unavailable on a tie", better_status(UNAVAILABLE, AVAILABLE), AVAILABLE)
check("unknown never wins", better_status(AVAILABLE, UNKNOWN), AVAILABLE)

print("\nNew-listing diffing")
s = fresh()
r4 = Reading(source="t", resale=AVAILABLE, listings=[Listing("Resale A", "€366.39", "resale")])
check("first sighting is new", st.record_success(s, r4), ["Resale A — €366.39"])
check("second sighting is not", st.record_success(s, r4), [])

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
