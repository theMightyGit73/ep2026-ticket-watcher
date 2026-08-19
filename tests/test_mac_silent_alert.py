"""The one alert written somewhere other than the Mac, and the beacon it reads.

On 2026-08-19 at 19:41 UTC David received "your Mac watcher has gone quiet".
The Mac was fine: pid 62590, up two and a half hours, 800 checks completed,
last poll six minutes earlier. Two separate defects produced that email and
made it unusable.

  1. THE BEACON HAD EXHAUSTED ITS QUOTA. The Mac published a heartbeat on
     every handled reading — 56 of them at 3-4 minute gaps — until ntfy began
     answering 429, after which nothing got through for 2.8 hours. Because the
     watcher kept retrying every few minutes, the limiter stayed empty. The
     dead man's switch judges the Mac by beacon age, so it declared a healthy
     watcher dead. Worse: that quota is shared with the ticket alert, so for
     those three hours a real listing could not have reached the phone either.

  2. THE FIX IT PRINTED WAS A PATH ON A GITHUB RUNNER. This alert is composed
     on Actions, where config.REPO_DIR is the runner's checkout, so the email
     said `cd /home/runner/work/ep2026-ticket-watcher/...`. It is the one
     alert that arrives when David is away from the machine and can act only
     on what the message says.

Run with:  .venv/bin/python tests/test_mac_silent_alert.py
"""

import smtplib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, liveness, notify  # noqa: E402

failures = []
sent = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): sent.append(msg)


smtplib.SMTP_SSL = FakeSMTP
notify.requests = type("_NoPush", (), {"post": staticmethod(lambda *a, **kw: None)})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "x"
config.NTFY_TOPIC = None


print("\nThe alert must never print the path of the machine that wrote it")
# Simulate being on a runner: REPO_DIR is the checkout, which on Actions is
# under /home/runner/work. The email must ignore it.
was_repo = config.REPO_DIR
try:
    config.REPO_DIR = Path("/home/runner/work/ep2026-ticket-watcher/ep2026-ticket-watcher")
    sent.clear()
    notify.mac_watcher_silent(2.8)
finally:
    config.REPO_DIR = was_repo

check("the email was sent", len(sent), 1)
body = sent[0].get_payload()[0].get_payload(decode=True).decode("utf-8")
check("and carries no runner path at all", "/home/runner" in body, False)
check_true("it names the Mac's own checkout instead", config.MAC_REPO_DIR in body)
check_true("with doctor as the first thing to run", "run_watcher.sh doctor" in body)
check_true("and restart.sh as the escalation", "restart.sh" in body)

print("\nAnd it must not state more than it knows")
# "No heartbeat arrived" is not "the Mac is off". Saying the stronger thing is
# what made this a false alarm rather than a useful warning, and David needs a
# way to tell the two apart from a phone.
check_true("it explains that a missing heartbeat may be the channel, not the Mac",
           "ntfy" in body.lower())
check_true("and gives a test he can apply without getting up",
           "hourly" in body.lower())
check_true("while still saying how long it has been quiet", "2.8 hours" in body)


print("\nThe beacon: throttled, so it cannot eat the alert channel")
per_day = 24 * 60 / config.LIVENESS_INTERVAL_MINUTES
print(f"  ({config.LIVENESS_INTERVAL_MINUTES:.0f} min apart = {per_day:.0f} beacons a day)")
check_true(f"far fewer than the ~450/day that exhausted the quota", per_day <= 150)
# It still has to be frequent enough that the switch is not jumpy.
per_window = (config.MAC_SILENT_HOURS * 60) / config.LIVENESS_INTERVAL_MINUTES
check_true(f"but at least four fit in the {config.MAC_SILENT_HOURS}h silence "
           f"window (got {per_window:.0f})", per_window >= 4)


print("\nA 429 must stop the beacon for a long while, not provoke a retry")
# The reason the block lasted 2.8 hours rather than minutes: a limiter
# refills over time, and continuous requests hold it empty.
class FakeResponse:
    def __init__(self, status): self.status_code = status


calls = []


def publishes(status):
    def post(url, **kw):
        calls.append(url)
        return FakeResponse(status)
    return post


was_topic, was_next, was_requests = (
    config.NTFY_TOPIC, liveness._next_allowed, liveness.requests)
try:
    config.NTFY_TOPIC = "test-topic"
    liveness.requests = type("_R", (), {
        "post": staticmethod(publishes(429)),
        "RequestException": Exception,
    })()
    liveness._next_allowed = 0.0
    check("a refused publish reports failure", liveness.publish("x"), False)
    check("it did try once", len(calls), 1)

    ordinary = time.time() + config.LIVENESS_INTERVAL_MINUTES * 60 + 1
    check("and is NOT due again at the ordinary interval",
          liveness.due(ordinary), False)
    cooled = time.time() + config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES * 60 + 1
    check("only after the cooldown", liveness.due(cooled), True)
    check_true("which is much longer than the ordinary interval",
               config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES
               > config.LIVENESS_INTERVAL_MINUTES)

    # A success behaves normally.
    liveness.requests = type("_R", (), {
        "post": staticmethod(publishes(200)),
        "RequestException": Exception,
    })()
    liveness._next_allowed = 0.0
    calls.clear()
    check("a good publish reports success", liveness.publish("x"), True)
    check("and holds off only for the ordinary interval",
          liveness.due(time.time() + config.LIVENESS_INTERVAL_MINUTES * 60 + 1), True)

    # And the throttle must genuinely suppress the extra sends.
    liveness._next_allowed = 0.0
    calls.clear()
    for _ in range(20):
        liveness.publish("x")
    check("twenty polls in a row produce exactly one beacon", len(calls), 1)
    # ...unless something explicitly needs to prove the path works.
    calls.clear()
    check("but `force` always sends, for the health check",
          liveness.publish("x", force=True), True)
    check("which really did publish", len(calls), 1)
finally:
    config.NTFY_TOPIC = was_topic
    liveness._next_allowed = was_next
    liveness.requests = was_requests


print("\ndoctor must call a 429 what it is, not a broken topic")
# The fix printed for every other push failure is "check NTFY_TOPIC", which
# for a rate limit sends David to edit a setting that is perfectly correct.
was_requests = notify.requests


class Rejecting:
    RequestException = Exception

    @staticmethod
    def post(*a, **kw):
        return FakeResponse(429)


try:
    config.NTFY_TOPIC = "test-topic"
    notify.requests = Rejecting()
    ok, detail = notify.verify_push()
finally:
    notify.requests = was_requests
    config.NTFY_TOPIC = None

check("a rate-limited push is not reported as working", ok, False)
check_true("the detail names rate limiting", "rate-limit" in detail.lower())
check_true("says the topic is fine", "topic is fine" in detail.lower())
check_true("and that email is unaffected", "email" in detail.lower())
check("without blaming the configuration", "check NTFY_TOPIC" in detail, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
