"""An alert nobody received has not been sent.

This is the failure that happened for real on 2026-08-18. A power cut took the
house network down at 09:00 UTC. At 09:39 four consecutive failures tripped the
watchdog, which tried both channels and lost both — the outage WAS the network,
so the Gmail send and the ntfy push each died on DNS resolution:

    [09:39] WARNING: watchdog-email notification failed: nodename nor servname
    [09:39] WARNING: watchdog-push notification failed: ... ntfy.sh ...

_maybe_watchdog() then stamped last_watchdog_alert anyway, because _safe()
swallows the exception and nobody asked whether anything arrived. That starts
the six-hour re-nag clock, so the failures at 09:48 and 09:57 raised nothing.
Power came back at 10:09 and it recovered on its own — but had the cut lasted,
the watcher would have sat silent for six hours believing it had already raised
the alarm.

The same applied to the hourly report: the 09:22 send failed, reset_heartbeat()
ran regardless, and that hour was simply lost.

That is the founding failure of this project — silence that means "fine" and
silence that means "broken", indistinguishable — reappearing one layer below
the alerting logic, in delivery.

Run with:  .venv/bin/python tests/test_alert_delivery.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import UNAVAILABLE, Reading  # noqa: E402

failures = []
sent = []
#: Flipped to simulate the network being down under the sending code.
network_up = {"email": True, "push": True}


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeSMTP:
    def __init__(self, *a, **kw):
        if not network_up["email"]:
            raise OSError("[Errno 8] nodename nor servname provided, or not known")

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): sent.append(msg)


def fake_post(*a, **kw):
    if not network_up["push"]:
        raise RuntimeError("Failed to resolve 'ntfy.sh'")
    return type("_Resp", (), {"status_code": 200, "raise_for_status": lambda self: None})()


smtplib.SMTP_SSL = FakeSMTP
notify.requests = type("_R", (), {"post": staticmethod(fake_post)})()
# The liveness beacon has to be stubbed too, and this file was the only one in
# the suite that missed it. engine.handle() publishes a heartbeat on every
# call, so with a topic configured and the real `requests` still in place,
# every run of these tests made live POSTs to ntfy.sh.
#
# That is not merely impolite. ntfy.sh allows an anonymous sender a fixed
# number of messages a day per IP, and on 2026-08-19 the watcher ran out of
# them — with repeated runs of this suite contributing to the total. The
# suite then failed, because a real 429 came back and the watcher correctly
# emailed to say push had stopped, which this file counted as an unexpected
# alert. A test that spends the production budget can also break itself.
from ep_watcher import liveness  # noqa: E402

liveness.requests = type("_R", (), {"post": staticmethod(fake_post)})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = "test-topic"

A = config.EVENTS[0]


def fresh():
    return dict(st._defaults())


def offline(state=True):
    network_up["email"] = network_up["push"] = not state


print("\n_safe reports whether the send actually worked")

offline(False)
check("a send that lands is True", notify._safe("t", lambda: None), True)
offline(True)
check("a send that raises is False",
      notify._safe("t", lambda: (_ for _ in ()).throw(OSError("no dns"))), False)

print("\nThe watchdog only starts its clock when the news got out")

offline(True)
s = fresh()
s["consecutive_failures"] = config.WATCHDOG_FAILURE_THRESHOLD
broken = Reading(source="browser", event_slug=A.slug, failed=True)
broken.note("Page.goto: net::ERR_INTERNET_DISCONNECTED")
engine._maybe_watchdog(broken, s, config.WATCHDOG_FAILURE_THRESHOLD)
check("undelivered leaves the clock unset", s["last_watchdog_alert"], None)
check_true("so the very next poll tries again", st.should_alert_watchdog(s))

offline(False)
sent.clear()
engine._maybe_watchdog(broken, s, config.WATCHDOG_FAILURE_THRESHOLD)
check_true("delivered stamps the clock", s["last_watchdog_alert"] is not None)
check("and it goes quiet again", st.should_alert_watchdog(s), False)
check("one email went out", len(sent), 1)

print("\nAn undelivered hourly report does not throw the hour away")

offline(True)
s = fresh()
s["last_heartbeat"] = (st.utc_now() - __import__("datetime").timedelta(hours=2)).isoformat()
s["checks_since_heartbeat"] = 12
before = s["last_heartbeat"]
engine._maybe_heartbeat(Reading(source="t", primary=UNAVAILABLE, resale=UNAVAILABLE), s)
check("the clock keeps running", s["last_heartbeat"], before)
check("and the counts are kept for the retry", s["checks_since_heartbeat"], 12)
check_true("so it is still due", st.should_send_heartbeat(s))

offline(False)
engine._maybe_heartbeat(Reading(source="t", primary=UNAVAILABLE, resale=UNAVAILABLE), s)
check_true("a delivered report resets the clock", s["last_heartbeat"] != before)
check("and clears the counts", s["checks_since_heartbeat"], 0)

print("\nRecovery is announced once, not once per watched page")

offline(False)
engine.network = type("_Net", (), {"public_ip": staticmethod(lambda *a, **kw: None), "fingerprint": staticmethod(lambda *a, **kw: {"key": None, "ip": None})})()
s = fresh()
for event in config.EVENTS:
    st.event_state(s, event.slug)["consecutive_failures"] = 6
st._sync_global_failures(s)
s["last_failure_reason"] = "this Mac had no internet connection"
s["outage_started_at"] = (
    st.utc_now() - __import__("datetime").timedelta(minutes=69)
).isoformat()

sent.clear()
good = [
    Reading(source="browser", event_slug=e.slug, event_name=e.name, event_url=e.url,
            primary=UNAVAILABLE, resale=UNAVAILABLE)
    for e in config.EVENTS
]
engine.handle(good[0], s)
first = len(sent)
for reading in good[1:]:
    engine.handle(reading, s)

check("only one recovery email for one recovery", len(sent), 1)
if len(config.EVENTS) > 1:
    check("and none while another page is still broken", first, 0)

body = sent[-1].get_payload()[0].get_payload(decode=True).decode("utf-8")
check_true("it says how long the watcher was dark", "69 minutes" in body)
check_true("and what the fault actually was", "no internet connection" in body)
check_true("and warns a listing could have been missed", "missed" in body.lower())


print("\nA push title must survive every character the alerts actually use")
# ntfy carries the title as an HTTP header, and headers are latin-1. Every
# character outside it raises inside requests before a byte leaves the machine,
# so the push does not arrive mangled — it never goes at all.
#
# This fired for real on 2026-08-20 at 09:44: an Early Entry listing at €46.50
# was found, the email went out, and the push died on the euro sign. The
# em-dash is the worse half, because it sits in FIXED titles rather than in
# data — including "EP2026: TICKET HELD — check out NOW", which means the most
# urgent push this system can send was guaranteed to fail every single time.
for raw in ("TICKET LIVE — €46.50 Early Entry",
            "EP2026: TICKET HELD — check out NOW",
            "HELD Verified Resale — €366.39 — TAP TO PAY",
            "[TEST — not real] anything at all"):
    safe = notify._header_safe(raw)
    try:
        safe.encode("latin-1")
        ok = True
    except UnicodeEncodeError:
        ok = False
    check_true(f"header-safe: {raw[:34]!r}", ok)

# Plain ASCII must pass through untouched, so the ordinary case stays readable
# in the log and in any client that does not decode encoded-words.
check("ascii titles are left alone",
      notify._header_safe("EP2026 watcher: every check failing"),
      "EP2026 watcher: every check failing")
check("and empty stays empty", notify._header_safe(""), "")

# The encoding is RFC 2047, which ntfy decodes back to the original. Verified
# against a live topic on 2026-08-20 — the title returned with its em-dash and
# euro sign intact.
import base64 as _b64  # noqa: E402
encoded = notify._header_safe("TICKET LIVE — €46.50")
check_true("non-ascii is wrapped as an RFC 2047 encoded-word",
           encoded.startswith("=?utf-8?B?") and encoded.endswith("?="))
check("and decodes back to exactly what went in",
      _b64.b64decode(encoded[len("=?utf-8?B?"):-2]).decode("utf-8"),
      "TICKET LIVE — €46.50")

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
