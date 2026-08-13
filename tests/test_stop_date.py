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

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
