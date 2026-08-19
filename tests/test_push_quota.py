"""The push allowance must be counted, and the beacon must yield it to alerts.

ntfy.sh gives an anonymous publisher a fixed number of messages a day per IP.
On 2026-08-19 the watcher spent them without noticing. The liveness beacon
published on every handled reading — 95 between 10:08 and 16:55 UTC, a rate of
336 a day against a limit of 250 — and at 16:55 the server began answering:

    {"code":42908,"http":429,"error":"limit reached: daily message quota
     reached; increase your limits with a paid plan, see https://ntfy.sh"}

Nothing on the machine knew. The first symptom was an email from GitHub five
hours later saying the Mac had gone quiet, about a watcher on its 800th check.
Throughout those five hours the push channel was dead — and that is the
channel a ticket alert travels on, against listings that live minutes.

Two properties are tested here. The spending has to be visible, and when the
day runs short the beacon has to stand down and leave the rest for messages
that actually say something. That is the rule David set for tickets — the
important thing wins the scarce resource — applied to the other scarce
resource in the system.

Run with:  .venv/bin/python tests/test_push_quota.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, liveness, pushquota  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


with tempfile.TemporaryDirectory() as tmp:
    was_state = config.STATE_FILE
    config.STATE_FILE = Path(tmp) / "state.json"
    try:
        print("\nCounting")
        check("a fresh day starts at zero", pushquota.used(), 0)
        check("with the whole allowance left",
              pushquota.remaining(), config.NTFY_DAILY_LIMIT)
        pushquota.note_sent()
        pushquota.note_sent(4)
        check("sends accumulate", pushquota.used(), 5)
        check("and the remainder falls", pushquota.remaining(),
              config.NTFY_DAILY_LIMIT - 5)
        check_true("the summary says both halves",
                   "5" in pushquota.summary()
                   and str(config.NTFY_DAILY_LIMIT) in pushquota.summary())

        print("\nThe server's refusal beats our own tally")
        # Our count only knows what this process sent since the counter
        # existed. On the day it was introduced ntfy was already refusing
        # while the local count stood at zero, so doctor reported 250 messages
        # remaining beside a line saying the quota was spent.
        pushquota.note_exhausted()
        check("a refusal marks the day spent", pushquota.remaining(), 0)
        check("and the count reconciles upward, never down",
              pushquota.used() >= config.NTFY_DAILY_LIMIT, True)

        print("\nThe beacon stands down before the alerts do")
        # The ordering that matters: the least important message must run out
        # of room first, and the most important must still have some.
        check("with the day spent, the beacon may not send",
              pushquota.may_send(config.NTFY_ALERT_RESERVE), False)
        check("and neither may anything else", pushquota.may_send(0), False)

        # Reset to a day that is nearly, but not quite, spent.
        (Path(tmp) / "push-quota.json").write_text(
            '{"day": "%s", "count": %d}'
            % (pushquota._today(), config.NTFY_DAILY_LIMIT
               - config.NTFY_ALERT_RESERVE + 1))
        check("inside the reserve, the beacon stands down",
              pushquota.may_send(config.NTFY_ALERT_RESERVE), False)
        check_true("but an alert may still be sent — that is the point",
                   pushquota.may_send(0))

        print("\nAnd the beacon really does check before publishing")
        sent = []
        was_topic, was_next, was_requests = (
            config.NTFY_TOPIC, liveness._next_allowed, liveness.requests)
        try:
            config.NTFY_TOPIC = "test-topic"
            liveness.requests = type("_R", (), {
                "post": staticmethod(lambda *a, **kw: (sent.append(1),
                                                       FakeResponse(200))[1]),
                "RequestException": Exception})()
            liveness._next_allowed = 0.0
            check("a beacon inside the reserve is not published",
                  liveness.publish("x"), False)
            check("nothing left the machine", sent, [])

            # With room, it publishes and counts itself.
            (Path(tmp) / "push-quota.json").write_text(
                '{"day": "%s", "count": 0}' % pushquota._today())
            liveness._next_allowed = 0.0
            check("with room it publishes", liveness.publish("x"), True)
            check("exactly once", len(sent), 1)
            check("and counts what it sent", pushquota.used(), 1)
        finally:
            config.NTFY_TOPIC, liveness._next_allowed, liveness.requests = (
                was_topic, was_next, was_requests)

        print("\nThe reserve has to be big enough to matter")
        check_true("some of the day is genuinely held back",
                   config.NTFY_ALERT_RESERVE > 0)
        check_true("but not so much that the beacon can never run",
                   config.NTFY_ALERT_RESERVE < config.NTFY_DAILY_LIMIT / 2)
        # And the routine traffic has to fit inside the allowance at all.
        beacon = 24 * 60 / config.LIVENESS_INTERVAL_MINUTES
        heartbeat = 24 / config.HEARTBEAT_HOURS
        check_true(
            f"routine traffic ({beacon + heartbeat:.0f}/day) leaves room for "
            f"alerts inside the {config.NTFY_DAILY_LIMIT}/day allowance",
            beacon + heartbeat < config.NTFY_DAILY_LIMIT
            - config.NTFY_ALERT_RESERVE)

        print("\nA corrupt or missing counter must not suppress alerts")
        (Path(tmp) / "push-quota.json").write_text("{ not json")
        check("unreadable reads as a fresh day", pushquota.used(), 0)
        check_true("so sending is still allowed", pushquota.may_send(0))
    finally:
        config.STATE_FILE = was_state


print("\nLosing the push channel must be reported — over the channel that works")
# The gap David asked to close. On 2026-08-19 the allowance went at 16:55 and
# nothing said anything: the first word he had was a FALSE "your Mac watcher
# has gone quiet" from GitHub at 21:42, five hours later. Email worked
# perfectly throughout and never mentioned it. So the outage now announces
# itself by email, which is the one path still open by definition.
import smtplib  # noqa: E402

from ep_watcher import notify  # noqa: E402

posted = []


class CapturingSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): posted.append(msg)


def subjects():
    return [m["Subject"] for m in posted]


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


smtplib.SMTP_SSL = CapturingSMTP
notify.requests = type("_NoPush", (), {"post": staticmethod(lambda *a, **kw: None)})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "x"

with tempfile.TemporaryDirectory() as tmp2:
    was_state = config.STATE_FILE
    config.STATE_FILE = Path(tmp2) / "state.json"
    try:
        posted.clear()
        pushquota.note_exhausted()
        check("running out of messages sends exactly one email", len(posted), 1)
        body = body_of(posted[0])
        check_true("it says push has stopped", "push" in subjects()[0].lower())
        check_true("and that email still works — the thing he needs to know",
                   "EMAIL" in body)
        check_true("it warns the GitHub 'gone quiet' alerts will be false",
                   "FALSE" in body)
        check_true("says securing is unaffected", "Securing is unaffected" in body)
        check_true("and names the permanent fix", "ntfy.sh" in body)

        # It must not become the next thing that floods the inbox.
        pushquota.note_exhausted()
        pushquota.note_exhausted()
        check("further refusals the same day stay quiet", len(posted), 1)

        print("\nAnd coming back is reported too")
        # Only a message that succeeds can prove a rate limit has lifted, so
        # recovery is noticed at the send, not on a timer.
        pushquota.note_sent()
        check("the first successful send reports recovery", len(posted), 2)
        check_true("saying push works again",
                   "working again" in subjects()[1].lower())
        check_true("and that the quiet-Mac emails in between were false",
                   "false" in body_of(posted[1]).lower())
        pushquota.note_sent()
        check("and it does not repeat", len(posted), 2)

        print("\nRecovery survives the day rolling over, because that is when it happens")
        # The allowance resets at a day boundary, so recovery almost always
        # falls on the following day. A flag cleared at midnight could never
        # report it.
        posted.clear()
        pushquota.note_exhausted()
        check("an outage is recorded", len(posted), 1)
        quota_file = Path(tmp2) / "push-quota.json"
        import json as _json
        data = _json.loads(quota_file.read_text())
        data["day"] = "2000-01-01"          # pretend a day has passed
        quota_file.write_text(_json.dumps(data))
        check("the new day starts with a clean count", pushquota.used(), 0)
        pushquota.note_sent()
        check("and recovery is still reported the next day", len(posted), 2)
        check_true("naming when it started", "working again" in subjects()[-1].lower())
    finally:
        config.STATE_FILE = was_state



print("\nAn allowance already known to be gone still reports itself")
# The gap that nearly swallowed the whole feature. Once the quota is known
# spent the beacon stops attempting, so no further 429 arrives — and an email
# that only fires on a refusal would never fire at all. Discovering it from
# our own count has to count as discovering it.
with tempfile.TemporaryDirectory() as tmp3:
    was_state = config.STATE_FILE
    config.STATE_FILE = Path(tmp3) / "state.json"
    was_topic, was_next, was_requests = (
        config.NTFY_TOPIC, liveness._next_allowed, liveness.requests)
    try:
        posted.clear()
        config.NTFY_TOPIC = "test-topic"
        attempts = []
        liveness.requests = type("_R", (), {
            "post": staticmethod(lambda *a, **kw: (attempts.append(1),
                                                   FakeResponse(200))[1]),
            "RequestException": Exception})()
        # Spend the day without ever seeing a 429 — as happens when the count
        # reaches the limit locally.
        (Path(tmp3) / "push-quota.json").write_text(
            '{"day": "%s", "count": %d}' % (pushquota._today(),
                                            config.NTFY_DAILY_LIMIT))
        liveness._next_allowed = 0.0
        check("the beacon stands down", liveness.publish("x"), False)
        check("without troubling the network", attempts, [])
        check("and the outage is reported by email anyway", len(posted), 1)
        check_true("saying push has stopped",
                   "stopped" in posted[0]["Subject"].lower())
        liveness._next_allowed = 0.0
        liveness.publish("x")
        check("and it still says it only once", len(posted), 1)
    finally:
        config.NTFY_TOPIC, liveness._next_allowed, liveness.requests = (
            was_topic, was_next, was_requests)
        config.STATE_FILE = was_state


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
