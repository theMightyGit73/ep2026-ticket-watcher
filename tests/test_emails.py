"""Check every email the watcher can send, without sending any of them.

SMTP is stubbed out, so this runs offline, needs no credentials, and can be
run as often as you like. It exists because the alert that matters arrives
exactly once, under time pressure: if it goes to the wrong address, or omits
the link to the page you need to open right now, you find out at the worst
possible moment. These checks make that a build-time failure instead.

Run with:  .venv/bin/python tests/test_emails.py
"""

import smtplib
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, notify, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading  # noqa: E402

failures = []
sent = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ── Stub SMTP so nothing leaves the machine ──────────────────────────────────

class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, msg):
        sent.append(msg)


smtplib.SMTP_SSL = FakeSMTP
notify.requests = type("_NoPush", (), {"post": staticmethod(lambda *a, **kw: None)})()

# Credentials must look present or _send_email refuses before building anything.
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = None


def last_body():
    """The decoded text of the most recent email.

    decode=True matters: any body containing a euro sign or an em dash is
    non-ASCII, so MIMEText base64-encodes it. Reading the raw payload gives
    you base64 and every substring assertion quietly fails — which is exactly
    what happened the first time these checks ran.
    """
    payload = sent[-1].get_payload()[0]
    return payload.get_payload(decode=True).decode("utf-8")


print("\nAvailability alert — the one that must not be wrong")

sent.clear()
notify.available(
    Reading(
        source="t",
        primary=UNAVAILABLE,
        resale=AVAILABLE,
        listings=[Listing("Verified Resale — Section STNDN1", "€366.39", "resale")],
    ),
    reason="resale went UNAVAILABLE → AVAILABLE",
    new_listings=["Verified Resale — Section STNDN1 — €366.39"],
)
check("exactly one email sent", len(sent), 1)
check("addressed to David", sent[-1]["To"], "davidcoyne73@gmail.com")
check_true("subject says tickets are available", "AVAILABLE" in sent[-1]["Subject"])
check_true("body links to the selling page", config.EVENT_URL in last_body())
check_true("body names the listing", "STNDN1" in last_body())
check_true("body shows the price", "366.39" in last_body())
check_true("body says which market", "resale" in last_body().lower())
# The alert has to survive contact with the app. A listing can be present and
# invisible: resale results are filtered by quantity, the page defaults to 2,
# and the panel only exists after a search on the website at all. Naming the
# ticket without saying how to reach it is half an alert.
body_now = last_body()
check_true("tells him to use a browser, not the app", "not the app" in body_now)
check_true("tells him the quantity to set", "Set the quantity" in body_now)
check_true("warns the page defaults to 2", "defaults to 2" in body_now)
check_true("names the panel to scroll to", "Verified Resale Tickets" in body_now)
# Reworded 2026-08-24. The email used to tell David a vanished listing was
# probably in somebody's basket and that such holds expire. The first half was
# a guess and the second was advice built on it; five days of chases waiting
# for those holds to lapse never saw one lapse. What is actually supported by
# the logs is the observation, not the mechanism: listings have been lasting
# around ten minutes and reappearing, so trying again is worth it whatever the
# reason. The instruction to David is the same; the invented cause is gone.
# Corrected again 2026-08-25, and this one was my error rather than the
# project's. The email said listings last "around ten minutes", which came
# from reading a CHASE duration as a listing's visibility. It is not what the
# data says: of 75 distinct listing ids, 70 were seen exactly once — gone by
# the next look — and the chase notes that looked like persistence actually
# read "still held after N checks", meaning the listing was ABSENT from the
# feed throughout. The advice that follows from the real number is the
# opposite of the advice that was being given.
check_true("tells him to move immediately", "GO NOW" in body_now)
check_true("and grounds that in the measurement",
           "gone by the watcher's next look" in body_now)
check_true("and no longer promises ten minutes",
           "ten minutes" not in body_now)
# And while the buyer is refused on every listing, the alert must not imply
# that securing is on its way — that buys hesitation exactly when it costs the
# ticket.
check_true("and tells him not to wait for the watcher",
           "do not wait for the watcher" in body_now.lower())

print("\nBasket alert — fired when a reserve actually succeeds")

sent.clear()
notify.reserved_in_browser(
    Reading(source="t", primary=AVAILABLE,
            listings=[Listing("General Admission (in basket)", "€310.50", "primary")])
)
check("one email sent", len(sent), 1)
check_true("subject conveys urgency", "now" in sent[-1]["Subject"].lower())
check_true("body links to the selling page", config.EVENT_URL in last_body())
check_true("body warns the hold expires", "expires" in last_body().lower())

print("\nHourly report")

sent.clear()
notify.heartbeat(checks=19, failures=0, hours=1.0,
                 reading=Reading(source="t", primary=UNAVAILABLE, resale=UNAVAILABLE))
check("one email sent", len(sent), 1)
check("addressed to David", sent[-1]["To"], "davidcoyne73@gmail.com")
check_true("says no luck yet", "no luck" in sent[-1]["Subject"].lower())
check_true("says it will keep trying", "still trying" in last_body().lower())
check_true("reports how many checks ran", "19" in last_body())
check_true("links to the selling page", config.EVENT_URL in last_body())

# The important case: "no success this hour" must read differently when the
# reason for no success is that the watcher itself is broken.
sent.clear()
notify.heartbeat(checks=19, failures=19, hours=1.0,
                 reading=Reading(source="t"))
check_true("all-failed hour is called out explicitly",
           "EVERY check was unhealthy" in last_body())

print("\nConnection health reaches the inbox")

sent.clear()
notify.heartbeat(
    checks=19, failures=0, hours=1.0, reading=Reading(source="t"),
    health=st.connection_health(dict(st._defaults())),
)
check_true("healthy connection is reported", "Connection health [OK]" in last_body())

blocked_state = dict(st._defaults())
blocked_state["block_history"] = [st.utc_now().isoformat()] * 8
sent.clear()
notify.heartbeat(
    checks=19, failures=19, hours=1.0, reading=Reading(source="t"),
    health=st.connection_health(blocked_state),
)
body = last_body()
check_true("a blocked connection is flagged", "Connection health [BLOCKED]" in body)
check_true("...and says to stop the watcher", "Stop the watcher" in body)
check_true("...and points at mobile data", "mobile data" in body)

sent.clear()
notify.watchdog("rate limited", failures=4, health=st.connection_health(blocked_state))
check_true("the watchdog carries it too", "Connection health [BLOCKED]" in last_body())

print("\nWatchdog")

sent.clear()
notify.watchdog("Could not get a usable reading.", failures=4)
check("one email sent", len(sent), 1)
body = last_body()
# The recovery commands must be IN the email. An alert that says something is
# broken and then makes you go and look up how to fix it is half an alert,
# and this one arrives when the watcher is already not working.
check_true("body says how to diagnose", "doctor" in body)
check_true("body says how to restart", "restart.sh" in body)
check_true("body gives runnable paths", str(config.REPO_DIR) in body)

print("\nDrills must be unmistakable")
# A sample "watcher stopped" email once arrived saying "this is the last
# email you'll get from it" while the watcher was running perfectly. An
# alert you cannot distinguish from a rehearsal is worse than no rehearsal.
sent.clear()
notify.stopped(checks_total=0)
check_true("a real alert carries no test marker", "[TEST" not in sent[-1]["Subject"])

notify.TEST_MODE = True
sent.clear()
notify.stopped(checks_total=0)
check_true("a drill is flagged in the subject", "[TEST — not real]" in sent[-1]["Subject"])
sent.clear()
notify.available(Reading(source="t", resale=AVAILABLE), "drill", [])
check_true("...for availability alerts too", "[TEST — not real]" in sent[-1]["Subject"])
notify.TEST_MODE = False

sent.clear()
notify.available(Reading(source="t", resale=AVAILABLE), "real", [])
check_true("and the marker is cleared afterwards", "[TEST" not in sent[-1]["Subject"])

print("\nDelivery failures must never crash a run")

def explode(*a, **kw):
    raise RuntimeError("SMTP is down")


real_send = notify._send_email
notify._send_email = explode
try:
    notify.available(Reading(source="t", resale=AVAILABLE), "test", [])
    print("  PASS  a failing send is swallowed, not raised")
except Exception as exc:  # pragma: no cover - this is the thing being tested
    print(f"  FAIL  exception escaped: {exc}")
    failures.append("send failure escaped")
finally:
    notify._send_email = real_send

print("\nHourly gating")

s = dict(st._defaults())
check("no heartbeat before the clock starts", st.should_send_heartbeat(s), False)

st.start_heartbeat_clock(s)
check("still quiet immediately after starting", st.should_send_heartbeat(s), False)

s["last_heartbeat"] = (st.utc_now() - timedelta(hours=config.HEARTBEAT_HOURS + 0.01)).isoformat()
check("fires once the hour is up", st.should_send_heartbeat(s), True)

st.note_check(s, unhealthy=False)
st.note_check(s, unhealthy=True)
check("counts checks", s["checks_since_heartbeat"], 2)
check("counts failures separately", s["failures_since_heartbeat"], 1)

st.note_degraded(s, ["browser"])
st.note_resale_visibility(s, Reading(source="t", resale=UNKNOWN))
st.note_resale_visibility(s, Reading(source="t", resale=UNAVAILABLE))
check("counts partial polls", s["degraded_since_heartbeat"], 1)
check("counts resale-blind polls only when blind", s["resale_blind_since_heartbeat"], 1)

st.reset_heartbeat(s)
check("reset clears the counters", s["checks_since_heartbeat"], 0)
check("reset clears the coverage counters", st.coverage(s), (0, 0))
check("reset silences it again", st.should_send_heartbeat(s), False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
