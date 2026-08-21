"""The day/night switch must announce itself, and account for the session.

The watcher runs in two modes with different settings and used to cross
between them silently — one line in a log nobody reads. Two problems with
that. A watcher that quietly starts polling three times more slowly is one
whose behaviour you cannot reason about from the inbox, and every ambiguity
of that kind in this project has eventually cost something. And the hourly
heartbeat can only ever show an hour, so nothing ever reported what a whole
day or a whole night actually achieved.

So each crossing sends one email: what changed, why, and what the finished
session did — including what any listing actually was, since by the time the
summary arrives it has almost certainly sold.

Run with:  .venv/bin/python tests/test_session_summary.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
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

A, B = config.EVENTS[0], config.EVENTS[1]


def body():
    return sent[-1].get_payload()[0].get_payload(decode=True).decode("utf-8")


def reading(event, resale=UNAVAILABLE, listings=(), **kw):
    return Reading(
        source="stub", event_slug=event.slug, event_name=event.name,
        event_url=event.url, primary=UNAVAILABLE, resale=resale,
        listings=list(listings), **kw,
    )


print("\nCounting a session")

s = dict(st._defaults())
st.start_session(s, "day")
check("it knows which mode it is in", st.session(s)["mode"], "day")

st.note_session_poll(s, reading(A))
st.note_session_poll(s, reading(B, resale=UNKNOWN))
st.note_session_poll(s, reading(A, resale=UNKNOWN))
check("counts every page reading", st.session(s)["checks"], 3)
check("counts the resale-blind ones", st.session(s)["resale_blind"], 2)

st.note_session_poll(s, Reading(source="x", failed=True))
check("a failed read is unhealthy", st.session(s)["unhealthy"], 1)

partial = Reading(source="x", failed_sources=["browser"], answering_sources=["discovery"])
st.note_session_poll(s, partial)
check("so is a partial one", st.session(s)["unhealthy"], 2)
check("and it is counted as partial too", st.session(s)["degraded"], 1)

st.note_session_poll(s, Reading(source="x", blocked=True, failed=True))
check("blocks are counted", st.session(s)["blocks"], 1)

print("\nWhat turned up is kept, not just how many")

found = Listing("Verified Resale — Section STNDN1 (WEEKEND CAMPING)", "€366.39", "resale")
st.note_session_find(s, reading(A, AVAILABLE, [found]))
check("the find is counted", st.session(s)["finds"], 1)
check_true("and the listing itself is kept", "STNDN1" in st.session(s)["listings"][0])
check_true("with its price", "366.39" in st.session(s)["listings"][0])
check_true("and which page it was on", A.name in st.session(s)["listings"][0])

st.note_session_find(s, reading(A, AVAILABLE, [found]))
check("the same listing is not recorded twice", len(st.session(s)["listings"]), 1)

# State must not grow without limit over a fortnight of running.
for i in range(st.SESSION_LISTING_CAP + 10):
    st.note_session_find(s, reading(A, AVAILABLE, [Listing(f"Listing {i}", "€1.00", "resale")]))
check("kept listings are capped",
      len(st.session(s)["listings"]), st.SESSION_LISTING_CAP)
check_true("keeping the most recent", "Listing 29" in st.session(s)["listings"][-1])

print("\nStarting a new session clears the old totals")

st.start_session(s, "night")
check("mode flipped", st.session(s)["mode"], "night")
check("counters reset", st.session(s)["checks"], 0)
check("listings reset", st.session(s)["listings"], [])

print("\nWhat the switch says has changed")

night = engine.session_settings("night")
labels = [r[0] for r in night]
check_true("going to night changes the poll cycle", "Poll cycle" in labels)
for label, before, after in night:
    check(f"{label} actually differs", before != after, True)

# The search timeout is listed only when the two modes genuinely differ. They
# are equal now — the daytime ceiling was raised to the overnight one after
# daytime timeouts appeared on a mobile connection — and a "changed" row
# showing the same value on both sides is noise, so it must be absent.
check("an unchanged setting is not reported as a change",
      "Search timeout" in labels,
      config.NIGHT_SEARCH_TIMEOUT_SECONDS != config.SEARCH_TIMEOUT_SECONDS)

was = config.NIGHT_SEARCH_TIMEOUT_SECONDS
config.NIGHT_SEARCH_TIMEOUT_SECONDS = config.SEARCH_TIMEOUT_SECONDS * 2
try:
    check_true("...but a real difference is",
               "Search timeout" in [r[0] for r in engine.session_settings("night")])
finally:
    config.NIGHT_SEARCH_TIMEOUT_SECONDS = was

day = engine.session_settings("day")
check("the two directions are mirror images",
      [(l, a, b) for l, b, a in day], night)

# A row claiming a change that is not one would appear whenever the two modes
# are configured alike, and is exactly the noise that gets a section skimmed.
saved = config.NIGHT_POLL_SECONDS, config.NIGHT_SEARCH_TIMEOUT_SECONDS
config.NIGHT_POLL_SECONDS = 0
config.NIGHT_SEARCH_TIMEOUT_SECONDS = config.SEARCH_TIMEOUT_SECONDS
check("identical modes list no changes", engine.session_settings("night"), [])
config.NIGHT_POLL_SECONDS, config.NIGHT_SEARCH_TIMEOUT_SECONDS = saved

print("\nThe email itself")

s = dict(st._defaults())
st.start_session(s, "day")
for _ in range(20):
    st.note_session_poll(s, reading(A))
st.note_session_poll(s, reading(B, resale=UNKNOWN))
st.note_session_find(s, reading(A, AVAILABLE, [found]))

sent.clear()
notify.session_summary(
    st.session(s), to_mode="night", hours=16.5,
    settings=engine.session_settings("night"),
    next_change=engine._next_switch("night"),
    health=st.connection_health(dict(st._defaults())),
    events=st.event_summaries(s),
)
check("one email sent", len(sent), 1)
text = body()
check_true("subject says which way it switched", "overnight" in sent[-1]["Subject"].lower())
check_true("subject offers a summary", "summary" in sent[-1]["Subject"].lower())
check_true("body says settings changed", "SETTINGS CHANGED" in text)
check_true("...showing the before and the after", "→" in text)
check_true("...and when it changes back",
           f"{config.NIGHT_END_HOUR:02d}:00 local" in text)
check_true("...honestly, since the switch lands on the next poll",
           "first poll after" in text)
check_true("explains why overnight is slower", "asleep" in text)
check_true("reports how long the session ran", "16.5 hours" in text)
check_true("reports the page checks", "21" in text)
check_true("reports resale readability", "20/21" in text)
check_true("names what turned up", "STNDN1" in text)
check_true("with the price", "366.39" in text)
check_true("carries connection health", "Connection health" in text)
check_true("and both pages' last readings", B.name in text)
check_true("says the hourly report continues", "hourly" in text)

print("\nA quiet session must say so rather than look broken")

s = dict(st._defaults())
st.start_session(s, "night")
for _ in range(14):
    st.note_session_poll(s, reading(A))
sent.clear()
notify.session_summary(st.session(s), to_mode="day", hours=7.0,
                       settings=engine.session_settings("day"),
                       next_change=engine._next_switch("day"))
text = body()
check_true("says nothing appeared", "Nothing appeared" in text)
check_true("rather than leaving it blank", "Tickets found" in text)
check_true("and switches back to daytime", "daytime" in sent[-1]["Subject"].lower())

print("\nWhen the switch fires")

saved_hours = config.NIGHT_START_HOUR, config.NIGHT_END_HOUR

s = dict(st._defaults())
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 24     # always night
sent.clear()
check("the very first session opens silently", engine.maybe_switch_session(s), True)
check("...with no email for a session that never ran", len(sent), 0)
check("and the mode is recorded", st.session(s)["mode"], "night")

check("staying in the same mode does nothing", engine.maybe_switch_session(s), False)
check("...and sends nothing", len(sent), 0)

st.note_session_poll(s, reading(A))
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 0      # never night
sent.clear()
check("crossing into the other mode fires", engine.maybe_switch_session(s), True)
check("...and sends exactly one email", len(sent), 1)
check("the new session starts clean", st.session(s)["checks"], 0)
check("...in the new mode", st.session(s)["mode"], "day")

# The switch is found by comparing stored mode to current mode, not by
# catching the instant it happens — so a restart across the boundary still
# reports the finished session instead of swallowing it.
s2 = dict(st._defaults())
st.start_session(s2, "night")
st.note_session_poll(s2, reading(A))
sent.clear()
check("a restart across the boundary still reports",
      engine.maybe_switch_session(s2), True)
check("...sending the summary it would otherwise have lost", len(sent), 1)

print("\nThe API-only backstop must not send these")
# It runs one-shot on a GitHub runner twice an hour-ish. It has no cadence to
# change, and "your settings have changed" from it would be meaningless.

saved_browser = config.USE_BROWSER
config.USE_BROWSER = False
s3 = dict(st._defaults())
st.start_session(s3, "night")
st.note_session_poll(s3, reading(A))
config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 0
sent.clear()
check("no session switching without a browser", engine.maybe_switch_session(s3), False)
check("and no email", len(sent), 0)
config.USE_BROWSER = saved_browser

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved_hours

print("\nA dead SMTP server must not take down the switch")

def explode(*a, **kw):
    raise RuntimeError("SMTP is down")

real = notify._send_email
notify._send_email = explode
try:
    notify.session_summary(st.session(s), to_mode="night", hours=1.0,
                           settings=[], next_change="later")
    print("  PASS  a failing send is swallowed, not raised")
except Exception as exc:  # pragma: no cover - the thing being tested
    print(f"  FAIL  exception escaped: {exc}")
    failures.append("send failure escaped")
finally:
    notify._send_email = real

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
