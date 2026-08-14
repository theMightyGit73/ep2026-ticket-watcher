"""Check the one failure the Mac cannot report about itself: being off.

launchd restarts a crashed watcher; the local watchdog kicks a hung one.
Both assume the laptop is running. If it is shut, flat, or off the network,
every safeguard on it is off too and the emails simply stop — and a watcher
that has silently stopped looks exactly like a quiet Ticketmaster.

So the Mac publishes a heartbeat to its own ntfy topic, and the hourly
GitHub job — which runs nowhere near the Mac — shouts when that heartbeat
goes stale.

Two properties matter equally. It must alert when the Mac is genuinely down,
and it must NOT alert when it merely cannot tell, because an ntfy outage is
not a dead watcher and false alarms here would teach David to ignore the one
message that means his watcher is really gone.

Run with:  .venv/bin/python tests/test_dead_mans_switch.py
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
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = "unit-test-topic"


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


print("\nThe heartbeat goes somewhere separate")

check("its own topic", liveness.topic(), "unit-test-topic-alive")
saved = config.NTFY_TOPIC
config.NTFY_TOPIC = None
check("and does not exist without push configured", liveness.topic(), None)
check("publishing is a no-op then", liveness.publish(), False)
check("and age is unknowable", liveness.age_seconds(), None)
config.NTFY_TOPIC = saved

# A heartbeat topic David subscribes to would fire every few minutes and get
# the app muted — taking the ticket alert with it.
check("it is NOT the topic carrying ticket alerts",
      liveness.topic() != config.NTFY_TOPIC, True)


class FakeResponse:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status


def with_heartbeat_age(seconds):
    """Stub ntfy so age_seconds() sees a heartbeat `seconds` old."""
    ts = time.time() - seconds
    line = '{"event":"message","time":%d,"message":"alive"}' % int(ts)

    class Stub:
        RequestException = Exception

        @staticmethod
        def get(*a, **kw):
            return FakeResponse(line)

        @staticmethod
        def post(*a, **kw):
            return FakeResponse()

    liveness.requests = Stub


print("\nReading the heartbeat's age")

with_heartbeat_age(120)
age = liveness.age_seconds()
check("a recent heartbeat", 100 < age < 200, True)

with_heartbeat_age(4 * 3600)
age = liveness.age_seconds()
check("an old one", round(age / 3600), 4)


print("\nUnreadable is NOT the same as dead")


class Broken:
    RequestException = Exception

    @staticmethod
    def get(*a, **kw):
        raise Exception("ntfy unreachable")

    @staticmethod
    def post(*a, **kw):
        raise Exception("ntfy unreachable")


liveness.requests = Broken
check("an ntfy outage reads as unknown", liveness.age_seconds(), None)
check("and publishing fails quietly rather than raising", liveness.publish(), False)


class EmptyTopic:
    RequestException = Exception

    @staticmethod
    def get(*a, **kw):
        return FakeResponse("")


liveness.requests = EmptyTopic
check("no heartbeats at all reads as unknown", liveness.age_seconds(), None)


print("\nThe alert itself")

sent.clear()
notify.mac_watcher_silent(3.4)
check("one email", len(sent), 1)
check("to David", sent[-1]["To"], "davidcoyne73@gmail.com")
check_true("subject says the Mac is quiet", "gone quiet" in sent[-1]["Subject"])
body = body_of(sent[-1])
check_true("says how long", "3.4" in body)
check_true("explains why it comes from GitHub", "not from the Mac" in body)
check_true("warns cover is reduced", "cannot see a Verified Resale" in body)
check_true("gives the doctor command", "doctor" in body)
check_true("gives the restart command", "restart.sh" in body)

print("\nThresholds")

check("silence limit is above the poll interval",
      config.MAC_SILENT_HOURS * 3600 > config.POLL_INTERVAL_SECONDS, True)
check("and above the overnight interval",
      config.MAC_SILENT_HOURS * 3600 > config.NIGHT_POLL_SECONDS, True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
