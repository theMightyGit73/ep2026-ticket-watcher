"""Check the watcher actually stops, once, after the festival.

Nothing here stops on its own otherwise, and an unattended watcher outliving
its event is how you end up with a cron job still emailing you about a
festival two years gone. Three things have to hold:

  * it runs through the final day and stops the morning after
  * it says goodbye, because unexplained silence is the one thing this design
    refuses to be ambiguous about
  * it says goodbye exactly once, however many times it is restarted

Run with:  .venv/bin/python tests/test_stop_date.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, notify, state as st  # noqa: E402

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

REAL_NOW = st.utc_now


def at(date_str):
    """Pretend today is `date_str` for the duration of the next call."""
    import datetime

    y, m, d = (int(p) for p in date_str.split("-"))
    st.utc_now = lambda: datetime.datetime(y, m, d, 12, 0, tzinfo=datetime.timezone.utc)


print(f"\nStop date is {config.STOP_AFTER_DATE}")

at("2026-08-13")
check("runs well before", st.past_stop_date(), False)

at("2026-08-27")
check("runs the day before", st.past_stop_date(), False)

at("2026-08-28")
check("runs ON the final day — a ticket that morning is still usable",
      st.past_stop_date(), False)

at("2026-08-29")
check("stops the morning after", st.past_stop_date(), True)

at("2026-09-15")
check("still stopped later", st.past_stop_date(), True)

at("2027-03-01")
check("and the following year", st.past_stop_date(), True)

st.utc_now = REAL_NOW

print("\nAn empty stop date disables the whole mechanism")
saved = config.STOP_AFTER_DATE
config.STOP_AFTER_DATE = ""
check("never stops", st.past_stop_date(), False)
config.STOP_AFTER_DATE = saved

print("\nThe goodbye email")

sent.clear()
notify.stopped(checks_total=2016)
check("one email", len(sent), 1)
check("to David", sent[-1]["To"], "davidcoyne73@gmail.com")
body = sent[-1].get_payload()[0].get_payload(decode=True).decode("utf-8")
check_true("says it stopped", "shut" in body.lower())
check_true("says it is the last one", "last email" in body)
check_true("reports how much it did", "2016" in body)
check_true("gives the tidy-up commands", "launchctl unload" in body)
check_true("says how to watch a later event", "EP_STOP_AFTER" in body)

print("\nIt must not say goodbye twice")

state = dict(st._defaults())
check("state starts un-notified", state["stop_notified"], False)
state["stop_notified"] = True
check("and the flag persists", state["stop_notified"], True)


print("\nPages stop on their own dates, not only on the watcher's")
# Products on one festival do not all stop being worth buying together. The
# Early Entry Pass grants campsite access from 2pm on the Thursday; from the
# Friday it is worth nothing, while the weekend tickets are still worth
# having. With only a global stop date the watcher spent a whole day
# searching for an expired add-on, against a rate limit that has already
# blocked this connection nineteen times — and, since securing is armed for
# that page, could have opened the buying browser for one.
early = next(e for e in config.EVENTS if e.slug == "early-entry")
weekend = next(e for e in config.EVENTS if e.slug == "weekend-camping")

check("the pass is live on its own last day", early.expired("2026-08-27"), False)
check("and expired the next morning", early.expired("2026-08-28"), True)
check("the weekend ticket is not", weekend.expired("2026-08-28"), False)
check("a page with no date of its own never expires alone",
      weekend.expired("2027-01-01"), False)


print("\nAn expired page is dropped from the schedule, both ways in")


class FakeEvent:
    """Minimum an event needs to be scheduled, with a stop date of its own."""

    def __init__(self, slug, stop_after="", watch=True):
        self.slug = slug
        self.stop_after = stop_after
        self.watch = watch
        self.poll_seconds = 600
        self.poll_max_seconds = 600

    expired = config.Event.expired
    # Borrowed from the real class rather than reimplemented, so the fake
    # cannot drift from the rule the scheduler actually applies. The two ways
    # a page stops being searched — a date it has passed, and a switch
    # somebody flipped — go through this one predicate precisely so a caller
    # cannot honour one and forget the other.
    searchable = config.Event.searchable


gone = FakeEvent("expired-addon", stop_after="2020-01-01")
alive = FakeEvent("weekend")
off = FakeEvent("switched-off", watch=False)

# Neither has ever been polled, so both would otherwise be due immediately.
fresh = dict(st._defaults())
due = st.due_events(fresh, [gone, alive])
check("the live page is due", [e.slug for e in due], ["weekend"])

# And the stall guard must not rescue it either. A page nobody should ask
# about cannot be "overdue" — that was the path that would have kept polling
# it once its ordinary schedule stopped mattering.
stale = dict(st._defaults())
stale["events"] = {
    "expired-addon": {"last_polled_at": "2020-01-02T00:00:00+00:00",
                      "next_gap_seconds": 600},
    "weekend": {"last_polled_at": st.utc_now().isoformat(),
                "next_gap_seconds": 600},
}
check("and the stall guard does not resurrect the expired one",
      [e.slug for e in st.due_events(stale, [gone, alive])], [])

# A page switched off is dropped by the same filter and for the same reason.
# The difference between it and an expired one is only that somebody can
# switch it back; to the scheduler, and to the request budget, they are
# identical — neither may cost a single page load.
fresh = dict(st._defaults())
check("a switched-off page is not due either",
      [e.slug for e in st.due_events(fresh, [off, alive])], ["weekend"])
stale_off = dict(st._defaults())
stale_off["events"] = {
    "switched-off": {"last_polled_at": "2020-01-02T00:00:00+00:00",
                     "next_gap_seconds": 600},
    "weekend": {"last_polled_at": st.utc_now().isoformat(),
                "next_gap_seconds": 600},
}
check("and the stall guard does not rescue it after days off",
      [e.slug for e in st.due_events(stale_off, [off, alive])], [])
check("but it is not 'expired' — that word means finished for good",
      off.expired("2030-01-01"), False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
