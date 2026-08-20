"""Two ticket pages, watched independently.

Electric Picnic sells the same weekend twice: an ordinary Weekend Camping
ticket and a Weekend Camping Instalment Plan, on separate pages with separate
inventory and separate resale panels. A ticket can appear on one and not the
other, so both are watched.

The failure this guards against is subtle and total. Availability used to
live in one flat set of "last seen" values. Watching two pages against one
set means the quiet page constantly overwrites the busy one's history — so a
listing appears on page A, page B's poll resets last_resale to UNAVAILABLE,
and A's next poll sees a fresh edge and alerts again forever; or worse, A
alerts once and B's poll marks it seen so the next real listing is silent.

Run with:  .venv/bin/python tests/test_multi_event.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

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
engine.network = type("_Net", (), {"public_ip": staticmethod(lambda *a, **kw: "10.0.0.1"), "fingerprint": staticmethod(lambda *a, **kw: {"key": "10.0.0.1", "ip": "10.0.0.1"})})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = None

A, B = config.EVENTS[0], config.EVENTS[1]


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


def reading(event, resale=UNAVAILABLE, listings=()):
    return Reading(
        source="stub", event_slug=event.slug, event_name=event.name,
        event_url=event.url, primary=UNAVAILABLE, resale=resale,
        listings=list(listings),
    )


def run(r, state):
    sent.clear()
    engine.handle(r, state)
    return list(sent)


print("\nBoth pages are configured and distinct")

# Three since 2026-08-19, when the Early Entry Pass was added.
check("three events watched", len(config.EVENTS), 3)
check("with different slugs", A.slug != B.slug, True)
check("and different URLs", A.url != B.url, True)
check_true("the instalment plan is the second", "instalment" in B.url)

print("\nRequest volume does not grow with the event count")

# Two pages must not mean twice the hourly load, because that is what got the
# home IP flagged. Pages now have their own intervals rather than sharing a
# cycle, so the volume is the sum of their rates — and that sum is the number
# that has to stay controlled, whatever the split between them.
# The tick follows the shortest gap a page can DRAW, not its mean. Ticking at
# the mean would make the bottom half of every range unreachable.
# The tick is the resolution at which a due page is noticed, not a request
# rate — a tick with nothing due opens no page and touches no network. It was
# the shortest drawable gap until 2026-08-19, which quantised the real cadence
# upward: a 300s tick against a 300-540s target delivered 10-12 minute gaps.
check_true("the tick is fine enough to honour the shortest gap",
           config.poll_interval() <= min(e.poll_min_seconds for e in config.EVENTS))
# Raised from 12/hour on 2026-08-19, when David asked for 3-6 minutes on the
# standard page. This is the cost of that change, stated rather than hidden:
# the mean gap there fell from 360s to 270s. The 20/hour line is the one that
# actually matters — it is what drew a block in development — and this stays
# under it, but by less room than before.
# One number stopped describing the cadence on 2026-08-19, when the day was
# split into peak, off-peak and night. The instantaneous rate is what has to
# stay under the ~20/hour that drew a block; the daily total is what says
# whether the split actually cost anything.
check_true("the busiest hour stays under the ~20/hour that drew a block",
           config.peak_searches_per_hour() < 20)
check_true("and is not creeping towards it unnoticed",
           config.peak_searches_per_hour() <= 19)
check_true("the quiet hours really are quieter",
           config.searches_per_hour_at(3) < config.searches_per_hour_at(14))
# The peak window is a REDISTRIBUTION, not a raise. The day must not cost
# more than the flat 3-6 minute cadence it replaced (~274 searches).
# The binding constraint is the instantaneous rate above, not this; this
# exists so that adding a fourth page cannot pass unnoticed.
check_true("a day stays within reach of the flat cadence it replaced",
           config.searches_per_day() <= 300)
# The floor was 200 while three pages were searched. It came down with the
# Early Entry Pass on 2026-08-20, and the number is re-baselined rather than
# removed: its job is to catch a configuration that has quietly stopped
# polling — a typo'd interval, a filter that excludes everything — and a floor
# left at the old value would have failed for the one reason that is not a
# fault. Two pages at this cadence cost ~159 a day.
check_true("and is not so cheap that something has broken",
           config.searches_per_day() >= 120)

# What switching the pass on and off does to this budget is checked in
# test_page_budget.py, not here. Reloading config mid-file would undo the
# credentials and the fake SMTP set up at the top of this one, and the
# alerting checks below would then pass for the wrong reason — silently, since
# "no email was sent" is what half of them are asserting.

# Weighted by yield, not split evenly. Eight of the nine resale sightings
# between 13 and 18 August were on the standard page and one on the instalment
# plan, so an even split spent half the budget for an eighth of the return.
check_true("the productive page is searched more often",
           A.poll_seconds < B.poll_seconds)

print("\nOne page going quiet cannot silence the other")

state = dict(st._defaults())
listing = Listing("Verified Resale — Section STNDN9", "€366.39", "resale")

mails = run(reading(A, AVAILABLE, [listing]), state)
check("a find on page A alerts", len(mails), 1)
check_true("naming page A", A.name in body_of(mails[0]))
check_true("and linking to page A", A.url in body_of(mails[0]))

# Page B polls straight after, sees nothing. It must not touch A's history.
run(reading(B), state)
mails = run(reading(A, AVAILABLE, [listing]), state)
check("A does not re-alert for the same listing", len(mails), 0)

print("\nAnd each page keeps its own history")

ev_a = st.event_state(state, A.slug)
ev_b = st.event_state(state, B.slug)
check("A remembers its listing", ev_a["last_resale"], AVAILABLE)
check("B remembers its own emptiness", ev_b["last_resale"], UNAVAILABLE)
check("A's listing is not attributed to B", ev_b["known_listings"], [])

print("\nA find on the second page alerts in its own right")

mails = run(reading(B, AVAILABLE, [Listing("Verified Resale — Section X1", "€410.00", "resale")]),
            state)
check("page B alerts too", len(mails), 1)
body = body_of(mails[0])
check_true("naming page B", B.name in body)
check_true("and linking to page B", B.url in body)
check_true("not page A's link", A.url not in body)

print("\nThe alert must never name the wrong event")

r = reading(B, AVAILABLE, [listing])
check("reading carries its own event", r.event_name, B.name)
sent.clear()
notify.available(r, "test", [])
check_true("email names it", B.name in body_of(sent[-1]))
check_true("subject names it", B.name in sent[-1]["Subject"])

print("\nA find must be recorded in the log, not merely counted")
# The log said "1 verified-resale listing(s) on the page" and nothing more,
# so the section and price lived only in the email. A real listing appeared
# on 2026-08-17 at 07:49 and lasted about fifteen minutes; afterwards there
# was no way to answer "what was it, and what did it cost?" from the log at
# all. That is the record you consult when deciding whether the next one is
# worth dropping everything for.

import io                     # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

state = dict(st._defaults())
found = Listing("Verified Resale — Section STNDN1 (WEEKEND CAMPING)", "€366.39", "resale")

buf = io.StringIO()
with redirect_stdout(buf):
    engine.handle(reading(A, AVAILABLE, [found]), state)
logged = buf.getvalue()

check_true("the log names the section", "STNDN1" in logged)
check_true("...and the price", "366.39" in logged)
check_true("...and which market it was on", "resale:" in logged)
check_true("...and which page", A.slug in logged)
check_true("the summary line is still there", "resale=AVAILABLE" in logged)

# A quiet poll must not start printing empty listing lines every ten minutes.
buf = io.StringIO()
with redirect_stdout(buf):
    engine.handle(reading(B), state)
check("a poll with nothing found adds no listing lines",
      "→" in buf.getvalue(), False)

print("\nThe cycle picks the most significant reading")

quiet = reading(A)
found = reading(B, AVAILABLE, [listing])
blocked = Reading(source="browser", event_slug=A.slug, failed=True, blocked=True)
check("a find outranks a quiet page", engine._most_significant([quiet, found]).event_slug, B.slug)
check("a find outranks a block", engine._most_significant([blocked, found]).event_slug, B.slug)
check("a block outranks plain quiet", engine._most_significant([quiet, blocked]).blocked, True)
check("nothing at all is still a reading", engine._most_significant([]).failed, True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
