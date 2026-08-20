"""Moving between connections must be confirmed in writing, at the time.

Which connection the watcher is using decides where a rate-limit block lands,
and the burnt connection is the one David must not try to buy on. That made
the switch worth an email of its own: until now it was logged and then
mentioned in the hourly report, up to an hour later, in a section that also
says "no luck yet".

Two things that look identical in the state file are told apart here. Moving
between home Wi-Fi and the hotspot is something David did. A hotspot being
issued a fresh address is something his carrier did — it was seen happening
on 2026-08-17 — and calling that "you switched networks" would be wrong, as
well as repeatable often enough to fill an inbox.

Run with:  .venv/bin/python tests/test_network_email.py
"""

import smtplib
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import UNAVAILABLE, Reading  # noqa: E402

failures = []
sent = []

HOME = "86.44.208.194"
HOTSPOT = "212.129.87.241"
HOTSPOT2 = "212.129.87.187"


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
config.NTFY_TOPIC = None
config.HOME_NETWORK_IP = HOME

A = config.EVENTS[0]


def body():
    return sent[-1].get_payload()[0].get_payload(decode=True).decode("utf-8")


def on(ip, state=None):
    """Put the watcher on `ip`, as a poll would."""
    s = state if state is not None else dict(st._defaults())
    st.note_network(s, ip)
    return s


def poll_on(s, seen):
    """One handle() cycle with the connection pinned, capturing any email.

    `seen` is either a bare address — the shape an older state file and an
    API-only host both have, where the address IS the identity — or a full
    fingerprint, where the gateway identifies the connection and the address
    is free to change underneath it.
    """
    fp = seen if isinstance(seen, dict) else {"key": seen, "ip": seen}
    engine.network = type("_Net", (), {
        "public_ip": staticmethod(lambda *a, **kw: fp.get("ip")),
        "fingerprint": staticmethod(lambda *a, **kw: dict(fp)),
    })()
    sent.clear()
    engine.handle(
        Reading(source="stub", event_slug=A.slug, event_name=A.name,
                event_url=A.url, primary=UNAVAILABLE, resale=UNAVAILABLE),
        s,
    )
    return list(sent)


print("\nA real switch between connections is emailed")

s = on(HOME)
mails = poll_on(s, HOTSPOT)
check("one email on switching", len(mails), 1)
text = body()
check_true("subject names the connection now in use", "hotspot" in sent[-1]["Subject"].lower())
check_true("body says what was left", "home Wi-Fi" in text)
check_true("...with its address", HOME in text)
check_true("body says what is now in use", HOTSPOT in text)
check_true("explains the counters reset", "from zero" in text)
check_true("and when the next switch is due", "switch again" in text)

print("\nIt fires once per change, not once per watched page")
# handle() runs per page. By the second, note_network has already recorded
# the new address, so it reports no change and must not mail again.

check("the second page of the same cycle is silent", len(poll_on(s, HOTSPOT)), 0)

print("\nThe first connection ever seen is not a 'switch'")

fresh = dict(st._defaults())
check("nothing is emailed on the very first poll", len(poll_on(fresh, HOME)), 0)
check("...but it is recorded", fresh["current_ip"], HOME)

print("\nA failed IP lookup must not read as a switch")

s = on(HOME)
check("no email when the lookup returns nothing", len(poll_on(s, None)), 0)
check("and the known connection is kept", s["current_ip"], HOME)

print("\nLeaving a flagged connection says so")

s = dict(st._defaults())
st.note_network(s, HOME)
for n in range(3):
    # Spaced: record_block() counts episodes now, not page readings.
    st.record_block(s, when=st.utc_now() - timedelta(minutes=30 - n * 5))
mails = poll_on(s, HOTSPOT)
check("switching away still emails", len(mails), 1)
text = body()
check_true("it names what the old connection collected", "3 block(s)" in text)
check_true("...and which one that was", "home Wi-Fi" in text)
check_true("...and says to let it recover", "recover" in text)

print("\nSwitching ONTO a flagged connection shouts")
# The one case here that needs acting on. Switching is meant to buy a clean
# connection; landing on a burnt one silently would defeat the whole scheme.

s = dict(st._defaults())
st.note_network(s, HOTSPOT)
for n in range(8):
    st.record_block(s, when=st.utc_now() - timedelta(minutes=50 - n * 5))
st.note_network(s, HOME)
sent.clear()
engine._announce_network(s, "switched", was_ip=HOTSPOT,
                        was_label="phone hotspot", was_blocks=8)
# HOME is clean, so the health block should be calm...
check_true("a clean destination is not alarming", "CAUTION" not in sent[-1]["Subject"])

s = dict(st._defaults())
st.note_network(s, HOME)
for n in range(8):
    st.record_block(s, when=st.utc_now() - timedelta(minutes=50 - n * 5))
sent.clear()
engine._announce_network(s, "switched", was_ip=HOTSPOT,
                        was_label="phone hotspot", was_blocks=0)
check_true("landing on a rate-limited connection is in the subject",
           "CAUTION" in sent[-1]["Subject"])
check_true("...and the body carries the full instructions",
           "mobile data" in body())

print("\nA new address on the SAME connection is told differently")
# The carrier re-addressing a tether is not something David did.

# Same router, new public address. That is what the carrier actually does,
# and it is only distinguishable from a real switch because the gateway is
# the identity — the old code compared labels, so it called this a switch
# whenever the two addresses happened to be labelled differently, and called
# a genuine switch a re-address whenever they were labelled the same.
TETHER = {"key": "aa:bb:cc:dd:ee:01", "gateway": "172.20.10.1",
          "port": "Wi-Fi", "subnet": "172.20.10.x", "hotspot": True}

s = dict(st._defaults())
st.note_network(s, dict(TETHER, ip=HOTSPOT))
mails = poll_on(s, dict(TETHER, ip=HOTSPOT2))
check("it is still reported", len(mails), 1)
text = body()
check_true("but not as a switch he made", "switched" not in text.lower())
check_true("it says the network reassigned it", "new address" in text.lower())
check_true("and that nothing needs doing", "nothing needs doing" in text.lower())
check_true("subject says the same", "new address" in sent[-1]["Subject"].lower())

print("\nA flapping tether must not fill the inbox")

check("a re-address just after the last one is suppressed",
      st.should_email_network(s, readdressed=True), False)

s["last_network_email_at"] = (
    st.utc_now() - timedelta(minutes=st.READDRESS_EMAIL_MIN_MINUTES + 1)
).isoformat()
check("...but is told again once the gap has passed",
      st.should_email_network(s, readdressed=True), True)

# A genuine switch is never suppressed, however recent the last email.
s["last_network_email_at"] = st.utc_now().isoformat()
check("a real switch is never held back",
      st.should_email_network(s, readdressed=False), True)

fresh = dict(st._defaults())
check("and the first re-address is not held back either",
      st.should_email_network(fresh, readdressed=True), True)

# The quiet period has to be long enough to actually be one. Ten minutes was
# not: once the watcher moved onto a mobile connection on 2026-08-18, the
# carrier re-addressed it at 10:35, 10:48 and 11:26 — three emails in fifty
# minutes, none of them about anything David had done or could act on.
check("the quiet period outlasts a churning tether",
      st.READDRESS_EMAIL_MIN_MINUTES >= 30.0, True)

print("\nThe API-only backstop has no network to speak of")

saved = config.USE_BROWSER
config.USE_BROWSER = False
s = on(HOME)
check("no network email without a browser", len(poll_on(s, HOTSPOT)), 0)
config.USE_BROWSER = saved

print("\nA dead SMTP server must not take down the poll")

def explode(*a, **kw):
    raise RuntimeError("SMTP is down")

real = notify._send_email
notify._send_email = explode
try:
    s = on(HOME)
    poll_on(s, HOTSPOT)
    print("  PASS  a failing send is swallowed, not raised")
except Exception as exc:  # pragma: no cover - the thing being tested
    print(f"  FAIL  exception escaped: {exc}")
    failures.append("send failure escaped")
finally:
    notify._send_email = real

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
