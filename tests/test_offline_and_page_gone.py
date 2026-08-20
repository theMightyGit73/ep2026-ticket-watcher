"""Two faults that must reach a human in their own words.

Both are cases where "the watcher failed a check" is true but useless, because
the fix is nothing to do with Ticketmaster being busy.

  1. THE MAC IS OFF THE INTERNET. Every source fails to resolve or connect.
     Happened on 2026-08-18 during a power cut: six cycles in a row failed with
     net::ERR_INTERNET_DISCONNECTED and NameResolutionError. The generic
     "could not get a usable reading from Ticketmaster" points at the wrong
     thing entirely — the answer is a router, not patience.

  2. THE EVENT PAGE IS GONE. Ticketmaster answers 404. Retrying cannot fix a
     URL that has changed, and neither can backing off or resetting the
     browser profile, so this must escalate rather than be absorbed as one
     more failed read. Left undetected it is the quietest possible death: the
     watcher runs forever, faithfully, watching a page that no longer exists.

Also covers the third thing a 403 should do: end the cycle. A refusal is a
verdict on this client, not on this page, so polling the next page just earns
a second refusal and books a second resale-blind reading for one wall.

Run with:  .venv/bin/python tests/test_offline_and_page_gone.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, engine  # noqa: E402
from ep_watcher.model import UNAVAILABLE, UNKNOWN, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def failed_with(*notes):
    r = Reading(source="browser", failed=True)
    for n in notes:
        r.note(n)
    return r


print("\nNo internet is recognised as no internet")

# Verbatim from the log of the 2026-08-18 power cut.
disconnected = failed_with(
    "attempt 1: navigation failed: Page.goto: net::ERR_INTERNET_DISCONNECTED at "
    "https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping...",
)
dns = failed_with(
    "request failed: HTTPSConnectionPool(host='app.ticketmaster.com', port=443): "
    "Max retries exceeded ... NameResolutionError(\"Failed to resolve "
    "'app.ticketmaster.com'\")",
)
check_true("a disconnected browser", engine.looks_offline(disconnected))
check_true("a DNS failure from an API source", engine.looks_offline(dns))

# The distinction has to hold in the other direction too, or every ordinary
# Ticketmaster problem would be reported as a broken router.
blocked = failed_with("HTTP 403 — this client is rate-limited, not merely challenged.")
check("a 403 is not an offline Mac", engine.looks_offline(blocked), False)
check("nor is a healthy reading", engine.looks_offline(Reading(source="t")), False)

reason = engine.watchdog_reason(disconnected)
check_true("the alert says the Mac cannot reach the internet",
           "CANNOT REACH THE INTERNET" in reason)
check_true("it names the thing to check", "Wi-Fi" in reason or "hotspot" in reason)
check_true("and promises a follow-up when it recovers", "email you again" in reason)
check("the headline for the recovery email",
      engine.failure_headline(disconnected), "this Mac had no internet connection")

print("\nA missing page is escalated, not retried forever")

gone = Reading(source="browser", failed=True, page_gone=True)
gone.note("HTTP 404 — Ticketmaster says this event page does not exist.")
reason = engine.watchdog_reason(gone)
check_true("the alert leads with the page being missing",
           "COULD NOT BE FOUND" in reason)
check_true("it says the URL has probably changed", "URL" in reason)
check_true("and tells him where to look", "config.py" in reason)
check("a missing page outranks a plain failure",
      engine.failure_headline(gone), "the Ticketmaster event page could not be found")

# merge() has to carry it up, or a page-gone verdict from the browser is lost
# the moment another source answers alongside it.
merged = engine.merge([gone, Reading(source="discovery-api", primary=UNAVAILABLE)])
check_true("merge carries the verdict up", merged.page_gone)

print("\nA 403 ends the cycle instead of collecting a second one")

polled = []


def fake_poll(session=None, event=None):
    polled.append(event.slug)
    # The first page is refused; a second would be too, if we asked.
    return Reading(source="browser", event_slug=event.slug, event_name=event.name,
                   event_url=event.url, failed=True, blocked=True,
                   primary=UNKNOWN, resale=UNKNOWN)


real_poll, real_handle, real_load, real_save = (
    engine.poll, engine.handle, engine.state_mod.load, engine.state_mod.save)
engine.poll = fake_poll
engine.handle = lambda reading, st: None
engine.state_mod.load = lambda: dict(engine.state_mod._defaults())
engine.state_mod.save = lambda st: None
try:
    engine.run_once()
finally:
    engine.poll, engine.handle = real_poll, real_handle
    engine.state_mod.load, engine.state_mod.save = real_load, real_save

check("only the first page is polled", polled, [config.EVENTS[0].slug])
check("the rest of the cycle is skipped", len(polled), 1)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
