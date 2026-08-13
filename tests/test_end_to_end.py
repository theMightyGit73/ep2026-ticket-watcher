"""Drive the whole pipeline: a source finds a ticket, an email goes out.

Every other test covers one layer. This covers the join between them, which
is where the previous watcher actually died: each individual piece looked
reasonable, and the thing as a whole never alerted once in 44 days.

Sources are stubbed and SMTP is captured, so this runs offline and touches
Ticketmaster not at all. What it proves is the wiring — that a resale listing
appearing really does end up as an email carrying the link.

Run with:  .venv/bin/python tests/test_end_to_end.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402

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
config.NTFY_TOPIC = None


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


def run(reading, state):
    """One full engine cycle against a canned reading."""
    sent.clear()
    engine.handle(reading, state)
    return list(sent)


NOTHING = lambda: Reading(source="stub", primary=UNAVAILABLE, resale=UNAVAILABLE)  # noqa: E731
FOUND = lambda: Reading(  # noqa: E731
    source="stub", primary=UNAVAILABLE, resale=AVAILABLE,
    listings=[Listing("Verified Resale — Section STNDN1", "€366.39", "resale")],
)

print("\nThe path that matters: nothing, then a ticket appears")

state = dict(st._defaults())
mails = run(NOTHING(), state)
check("a quiet check sends nothing", len(mails), 0)

mails = run(FOUND(), state)
check("a resale listing sends exactly one email", len(mails), 1)
body = body_of(mails[0])
check("to David", mails[0]["To"], "davidcoyne73@gmail.com")
check_true("subject announces availability", "AVAILABLE" in mails[0]["Subject"])
check_true("body carries the link to buy", config.EVENT_URL in body)
check_true("body names the listing", "STNDN1" in body)
check_true("body has the price", "366.39" in body)

print("\nIt must not then repeat itself every poll")

mails = run(FOUND(), state)
check("second identical find stays quiet", len(mails), 0)

print("\nBut a NEW listing on top of an existing one must alert again")

more = FOUND()
more.listings.append(Listing("Verified Resale — Section STNDN2", "€310.50", "resale"))
mails = run(more, state)
check("a newly appeared listing re-alerts", len(mails), 1)
check_true("and calls out what is new", "STNDN2" in body_of(mails[0]))

print("\nGoing quiet again re-arms the alert")

run(NOTHING(), state)
mails = run(FOUND(), state)
check("availability returning alerts afresh", len(mails), 1)

print("\nA blocked connection is never mistaken for 'no tickets'")

state = dict(st._defaults())
blocked = Reading(source="browser", failed=True, blocked=True)
for _ in range(config.WATCHDOG_FAILURE_THRESHOLD):
    mails = run(blocked, state)
check("repeated blocks raise the watchdog", len(mails), 1)
body = body_of(mails[0])
check_true("which explains it is rate limiting", "rate-limiting" in body.lower())
check_true("and reports connection health", "Connection health" in body)
check("blocks are recorded for the health check", st.recent_blocks(state, 1),
      config.WATCHDOG_FAILURE_THRESHOLD)
check("no false availability was recorded", state["last_resale"], "UNKNOWN")

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
