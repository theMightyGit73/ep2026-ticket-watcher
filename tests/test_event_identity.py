"""Every alert must say WHICH ticket it is about.

Electric Picnic sells the same weekend twice, on two pages with separate
inventory and separate resale panels:

  · Weekend Camping                 — pay in full
  · Weekend Camping Instalment Plan — pay in stages

The names differ only by a trailing suffix, which is easy to skim past on a
phone in the ninety seconds a resale listing survives. So an alert that names
or links the wrong one is not a cosmetic problem: it sends David to a page
with nothing on it while the real listing sells.

This was a live bug. available() took its name and link from the reading, but
reserved_in_browser() — the loudest alert the watcher can send, the one with
a checkout countdown running — still read them from config, so it always
described the standard page whichever page had actually reserved. The hourly
report was worse: it printed one page's statuses beneath the other page's
URL, with nothing to signal the mismatch. The only real find so far, on
2026-08-16, was on the instalment page.

These checks run against every configured event rather than a fixed one, so
adding a third page cannot quietly reintroduce the bug.

Run with:  .venv/bin/python tests/test_event_identity.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, notify, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading  # noqa: E402

# Every configured page, including the one switched off by default.
#
# The bug this file exists to prevent — an alert naming or linking the wrong
# page — gets MORE likely as pages are added, not less, so the checks run
# against all three whatever today's switches say. See
# config.WATCH_EARLY_ENTRY.
import importlib  # noqa: E402
import os  # noqa: E402


# The pass carries a stop_after of 2026-08-27, so once that date passed it
# became permanently expired and this file's per-page checks quietly dropped
# from three pages to two. Pinned forward: what is being tested is that every
# watched page gets its own summary, named and linked, which is a property of
# the code and not of the calendar.
os.environ["EP_EARLY_ENTRY_STOP_AFTER"] = "2099-12-31"
os.environ["EP_EARLY_ENTRY"] = "1"
importlib.reload(config)

failures = []
sent = []
pushed = []


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


class FakePush:
    """Records ntfy posts so the tap-through link can be checked.

    The push is the alert that actually arrives in time; if its Click header
    points at the wrong page, tapping it costs the ticket just as surely as a
    wrong link in the email.
    """

    @staticmethod
    def post(url, data=None, headers=None, **kw):
        pushed.append({"url": url, "data": data, "headers": headers or {}})


smtplib.SMTP_SSL = FakeSMTP
notify.requests = FakePush()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = "ep2026-test-topic"


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


def reading_for(event, **kw):
    """A reading that knows which page it came from, as the real ones do."""
    return Reading(
        source="browser",
        event_slug=event.slug,
        event_name=event.name,
        event_url=event.url,
        **kw,
    )


def others(event):
    """Every watched page except this one."""
    return [e for e in config.EVENTS if e.slug != event.slug]


print("\nThe two pages are genuinely distinguishable")

check("more than one page is watched", len(config.EVENTS) > 1, True)
names = [e.name for e in config.EVENTS]
urls = [e.url for e in config.EVENTS]
check("names are unique", len(set(names)), len(names))
check("URLs are unique", len(set(urls)), len(urls))
check_true("one of them is the instalment plan",
           any("instalment" in n.lower() for n in names))

print("\nThe availability alert — for every page, not just the first")

for event in config.EVENTS:
    sent.clear()
    pushed.clear()
    listing = Listing(f"Verified Resale — Section X ({event.slug})", "€366.39", "resale")
    notify.available(reading_for(event, primary=UNAVAILABLE, resale=AVAILABLE,
                                 listings=[listing]),
                     reason="resale went UNAVAILABLE → AVAILABLE",
                     new_listings=[listing.describe()])
    body = body_of(sent[-1])
    check_true(f"[{event.slug}] subject names this page", event.name in sent[-1]["Subject"])
    check_true(f"[{event.slug}] body names this page", event.name in body)
    check_true(f"[{event.slug}] body links to this page", event.url in body)
    for other in others(event):
        check(f"[{event.slug}] and never links to {other.slug}", other.url in body, False)
    # Since 2026-08-19 the availability push points at the event URL plus the
    # quantity, so David lands on a page already asking for one ticket rather
    # than on the default of two. What this check is really for is that the
    # push never sends him to the OTHER page — so it tests the prefix, and
    # then tests the thing it actually cares about explicitly.
    click = pushed[-1]["headers"].get("Click")
    check_true(f"[{event.slug}] push links to this page", click.startswith(event.url))
    check_true(f"[{event.slug}] push carries the quantity",
               f"quantity={config.WANTED_QUANTITY}" in click)
    for other in others(event):
        check(f"[{event.slug}] push never links to {other.slug}",
              other.url in click, False)

print("\nThe basket alert — the one with a countdown running")
# This is the alert that was broken: it named and linked config.EVENT_* no
# matter which page had actually put tickets in a basket.

for event in config.EVENTS:
    sent.clear()
    pushed.clear()
    notify.reserved_in_browser(
        reading_for(event, primary=AVAILABLE,
                    listings=[Listing("General Admission (in basket)", "€310.50", "primary")])
    )
    body = body_of(sent[-1])
    check_true(f"[{event.slug}] subject names this page", event.name in sent[-1]["Subject"])
    check_true(f"[{event.slug}] body names this page", event.name in body)
    check_true(f"[{event.slug}] body links to this page", event.url in body)
    for other in others(event):
        check(f"[{event.slug}] and never links to {other.slug}", other.url in body, False)
    check_true(f"[{event.slug}] push names this page", event.name in str(pushed[-1]["data"]))
    check_true(f"[{event.slug}] push links to this page",
               pushed[-1]["headers"].get("Click") == event.url)

print("\nEach alert spells out which KIND of ticket it is")
# "Instalment Plan" as a trailing suffix is easy to miss at speed, and the two
# are bought differently, so the email says it in its own words.

for event in config.EVENTS:
    instalment = "instalment" in event.name.lower()
    sent.clear()
    notify.available(reading_for(event, resale=AVAILABLE), "test", [])
    body = body_of(sent[-1]).lower()
    if instalment:
        check_true(f"[{event.slug}] says it is the instalment plan",
                   "instalment plan" in body)
        check_true(f"[{event.slug}] and says it is not the standard one",
                   "not the standard" in body)
    elif "early entry" in event.name.lower():
        # The distinction that matters most: acting on this one in a hurry
        # could mean buying something unusable. It is an add-on, valid only
        # alongside a Weekend Ticket.
        check_true(f"[{event.slug}] says it is an add-on", "add-on" in body)
        check_true(f"[{event.slug}] and says it is not a ticket",
                   "not a festival ticket" in body)
        check_true(f"[{event.slug}] and names what it needs alongside it",
                   "weekend ticket" in body)
    else:
        check_true(f"[{event.slug}] says it is the standard page", "standard" in body)
        check_true(f"[{event.slug}] and says it is not the instalment plan",
                   "not the instalment" in body)

print("\nThe hourly report covers every page, each with its own statuses")
# It used to print whichever single reading tripped the clock, under a link
# hardcoded to the first event — so it could show the instalment plan's
# statuses beneath the standard page's URL.

state = dict(st._defaults())
first, second = config.EVENTS[0], config.EVENTS[1]
st.event_state(state, first.slug).update(last_primary=UNAVAILABLE, last_resale=UNAVAILABLE)
st.event_state(state, second.slug).update(last_primary=UNAVAILABLE, last_resale=UNKNOWN)

summaries = st.event_summaries(state)
check("one summary per watched page", len(summaries), len(config.EVENTS))

sent.clear()
notify.heartbeat(checks=12, failures=0, hours=1.0,
                 reading=reading_for(second, primary=UNAVAILABLE, resale=UNKNOWN),
                 events=summaries)
body = body_of(sent[-1])
for event in config.EVENTS:
    check_true(f"names {event.slug}", event.name in body)
    check_true(f"links {event.slug}", event.url in body)

check_true("says the standard page could read resale",
           "Verified resale: UNAVAILABLE" in body)
check_true("and that the instalment page could not",
           "UNKNOWN — nothing could read this market" in body)
check_true("counts are labelled as per-page",
           f"across {len(config.EVENTS)} pages" in body)
check("subject does not claim to be about one page",
      first.name in sent[-1]["Subject"], False)
check_true("subject names the watch as a whole",
           config.WATCH_LABEL in sent[-1]["Subject"])

print("\nA reading with no event at all still produces a sane email")
# The API-only backstop on GitHub builds readings without event identity.
# It must fall back to the configured event rather than emailing a blank.

sent.clear()
notify.available(Reading(source="discovery-api", resale=AVAILABLE), "test", [])
body = body_of(sent[-1])
check_true("falls back to the configured event name", config.EVENT_NAME in body)
check_true("and its URL", config.EVENT_URL in body)

sent.clear()
notify.heartbeat(checks=1, failures=0, hours=1.0,
                 reading=Reading(source="discovery-api", primary=UNAVAILABLE))
check_true("the single-reading report still links somewhere",
           config.EVENT_URL in body_of(sent[-1]))

print("\nThe watcher-stopped email accounts for both pages")

sent.clear()
notify.stopped(checks_total=1234)
body = body_of(sent[-1])
for event in config.EVENTS:
    check_true(f"final email lists {event.slug}", event.name in body)

print("\nNo alert may name a page it was not about")
# The catch-all. Build a reading for each page, send every event-bearing
# alert, and assert no other page's name or URL appears anywhere in it.

leaks = []
for event in config.EVENTS:
    for label, send in (
        ("available", lambda r: notify.available(r, "test", [])),
        ("reserved", notify.reserved_in_browser),
    ):
        sent.clear()
        send(reading_for(event, primary=AVAILABLE, resale=AVAILABLE))
        text = body_of(sent[-1]) + " " + sent[-1]["Subject"]
        # The instalment page's name CONTAINS the standard page's name in
        # full, so a naive substring test reports a leak on every correct
        # instalment email. Remove this event's own name first and ask what
        # is left — the same whole-versus-part trap as the resale heading
        # containing the resale listing marker.
        remainder = text.replace(event.name, "")
        for other in others(event):
            if other.url in text:
                leaks.append(f"{label}/{event.slug} linked {other.slug}")
            if other.name in remainder:
                leaks.append(f"{label}/{event.slug} named {other.slug}")
check("no alert mentions the wrong page", leaks, [])

# Guard the guard: the check above only means something if it can fail.
sent.clear()
notify.available(reading_for(config.EVENTS[0], resale=AVAILABLE), "test", [])
wrong = body_of(sent[-1]).replace(config.EVENTS[0].name, "")
check("the leak check would catch a wrong link",
      config.EVENTS[1].url in body_of(sent[-1]), False)
check("...and is looking at real content", len(wrong) > 100, True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
