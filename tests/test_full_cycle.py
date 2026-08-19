"""Drive engine.run_once() itself — the loop, not just the pieces inside it.

tests/test_end_to_end.py calls engine.handle() with readings it invents, which
proves the alerting decisions. It does not prove the thing that surrounds
them: which pages get searched on this tick, what happens to the rest of the
cycle when one page is refused, whether a find really reaches the buyer, and
whether the state written at the end is the state the next tick reads.

That surround is where the expensive mistakes have been. A blocked page that
kept the cycle going earned a second 403. A recovery notice never sent because
pages recovered one at a time. A gap re-drawn on every tick, collapsing a
5-9 minute range to its floor. None of those are visible from inside a single
reading.

Everything here is offline: the browser is a stub that returns canned
readings, SMTP is captured, the network lookup is faked, and the state file is
a temporary one. Ticketmaster is not contacted.

Run with:  .venv/bin/python tests/test_full_cycle.py
"""

import smtplib
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer, config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402
from ep_watcher.sources import discovery, inventory_api  # noqa: E402

failures = []
sent = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ── the offline harness ──────────────────────────────────────────────────────

class FakeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): sent.append(msg)


smtplib.SMTP_SSL = FakeSMTP
notify.requests = type("_NoPush", (), {"post": staticmethod(lambda *a, **kw: None)})()
engine.network = type("_Net", (), {
    "public_ip": staticmethod(lambda *a, **kw: "10.0.0.1"),
    "fingerprint": staticmethod(lambda *a, **kw: {"key": "aa:bb", "ip": "10.0.0.1"}),
})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = None
config.USE_BROWSER = True
# The API sources would otherwise answer for real if a key happens to be in
# the environment of whoever runs this, which would make the result depend on
# the machine rather than on the code.
discovery.configured = lambda: False
inventory_api.configured = lambda: False

_tmpdir = tempfile.TemporaryDirectory()
config.STATE_FILE = Path(_tmpdir.name) / "state.json"

SLUGS = [e.slug for e in config.EVENTS]
BUSY, INSTALMENT, EARLY = SLUGS[0], SLUGS[1], SLUGS[2]


def nothing():
    return Reading(source="browser", primary=UNAVAILABLE, resale=UNAVAILABLE)


def found():
    return Reading(
        source="browser", primary=UNAVAILABLE, resale=AVAILABLE,
        listings=[Listing("Verified Resale — Section STNDN1", "€366.39",
                          "resale", listing_id="l27t4h2d", section="STNDN1")],
    )


def refused():
    reading = Reading(source="browser", primary="UNKNOWN", resale="UNKNOWN")
    reading.failed = True
    reading.blocked = True
    reading.note("HTTP 403 — this client is being rate limited")
    return reading


def broken():
    reading = Reading(source="browser", primary="UNKNOWN", resale="UNKNOWN")
    reading.failed = True
    reading.note("net::ERR_INTERNET_DISCONNECTED")
    return reading


class FakeBrowser:
    """Stands in for a warm BrowserSession. Records what it was asked."""

    def __init__(self):
        self.polled = []
        self.answers = {}

    def check(self, event):
        self.polled.append(event.slug)
        return self.answers.get(event.slug, nothing)()

    def close(self):
        pass


def make_due(state, *slugs):
    """Rewind these pages so the next tick considers them due."""
    for slug in slugs or SLUGS:
        ev = st.event_state(state, slug)
        ev["last_polled_at"] = (st.utc_now() - timedelta(hours=6)).isoformat()


def cycle(browser):
    """One tick of the watch loop, capturing whatever it sends."""
    sent.clear()
    reading = engine.run_once(browser)
    return reading, list(sent)


def subjects():
    return [m["Subject"] for m in sent]


# ── the cycle itself ─────────────────────────────────────────────────────────

print("\nA first tick searches every page, because none has been searched yet")
browser = FakeBrowser()
reading, mails = cycle(browser)
check("every configured page was searched", sorted(browser.polled), sorted(SLUGS))
check("a quiet cycle sends nothing", len(mails), 0)
check("and the cycle is not reported as failed", reading.failed, False)

print("\nThe very next tick searches nothing — pages wait out their own gaps")
# This is what makes a 60-second tick cost nothing. If an idle tick polled, the
# request budget would be set by the tick rather than by the per-page ranges,
# which is roughly 60 searches an hour instead of 18.
browser.polled.clear()
reading, mails = cycle(browser)
check("no page was searched", browser.polled, [])
check("the cycle reports itself idle", reading.source, "idle")
check("and it sends nothing", len(mails), 0)
state = st.load()
check("an idle tick is not counted as a check", state.get("checks_total"), 3)

print("\nA listing on one page alerts, and names that page rather than another")
state = st.load()
make_due(state)
st.save(state)
browser.polled.clear()
browser.answers = {EARLY: found}
reading, mails = cycle(browser)
check("exactly one email went out", len(mails), 1)
check_true("announcing availability", "AVAILABLE" in mails[0]["Subject"])
body = mails[0].get_payload()[0].get_payload(decode=True).decode("utf-8")
early_event = next(e for e in config.EVENTS if e.slug == EARLY)
check_true("and it names the page the listing was actually on",
           early_event.name in mails[0]["Subject"] or early_event.name in body)
check_true("with the section", "STNDN1" in body)
check_true("and the price", "366.39" in body)
check("run_once returns the find, not whichever page happened to be last",
      reading.event_slug, EARLY)

print("\nThe same listing on the next tick does not alert again")
state = st.load()
make_due(state)
st.save(state)
browser.answers = {EARLY: found}
reading, mails = cycle(browser)
check("no repeat email", len(mails), 0)

print("\nA 403 stops the cycle rather than earning a second one")
# A refusal is a verdict on this client, not on this page. Carrying on to the
# next page sends another request to an endpoint that has just refused us and
# books a second resale-blind reading for one wall.
state = st.load()
make_due(state)
st.save(state)
browser.polled.clear()
browser.answers = {BUSY: refused, INSTALMENT: found, EARLY: found}
reading, mails = cycle(browser)
check("the refused page was searched", browser.polled[0], BUSY)
check("and nothing after it was", len(browser.polled), 1)
check("the cycle reports the block", reading.blocked, True)
state = st.load()
check_true("and the block is recorded against the connection",
           st.recent_blocks(state, 24) >= 1)

print("\nA run of failures nags, and recovering says so")
browser.answers = {slug: broken for slug in SLUGS}
watchdogged = False
for _ in range(config.WATCHDOG_FAILURE_THRESHOLD + 1):
    state = st.load()
    make_due(state)
    st.save(state)
    _, mails = cycle(browser)
    if any("WATCHER" in (s or "").upper() or "BROKEN" in (s or "").upper()
           for s in [m["Subject"] for m in mails]):
        watchdogged = True
check_true("a sustained outage produces a watchdog email", watchdogged)

# Now everything comes back at once.
browser.answers = {}
state = st.load()
make_due(state)
st.save(state)
_, mails = cycle(browser)
recovered = [m for m in mails if "RECOVER" in (m["Subject"] or "").upper()
             or "working again" in (m["Subject"] or "").lower()]
check_true("and recovering sends a notice rather than going quiet", recovered)
state = st.load()
check("the outage bookkeeping is cleared, not left to outlive it",
      state.get("outage_started_at"), None)
check("including its peak", state.get("outage_peak_failures"), 0)

print("\nA find reaches the buyer, and the buyer's answer reaches the inbox")
attempts = []


def fake_secure(event, listing, timeout_s=None, may_preempt=False):
    attempts.append((event.slug, listing.section, may_preempt))
    hold = buyer.HoldResult(secured=True, minutes_hint=11, minutes_measured=True,
                            checkout_url="https://www.ticketmaster.ie/checkout/abc")
    return hold


was_flag, was_secure = config.SECURE_ON_FIND, buyer.secure_in_thread
try:
    config.SECURE_ON_FIND = True
    buyer.secure_in_thread = fake_secure
    state = st.load()
    make_due(state)
    # Clear the remembered listing, or this reads as the same one as before
    # and never reaches the alerting path at all.
    st.event_state(state, BUSY)["known_listings"] = []
    st.event_state(state, BUSY)["last_resale"] = UNAVAILABLE
    st.save(state)
    browser.answers = {BUSY: found}
    reading, mails = cycle(browser)
    check("the buyer was asked to hold exactly one listing", len(attempts), 1)
    check("for the page the listing was on", attempts[0][0], BUSY)
    check("and the listing it was actually told about", attempts[0][1], "STNDN1")
    check("with nothing to preempt, since nothing was held", attempts[0][2], False)
    subs = " | ".join(m["Subject"] or "" for m in mails)
    check_true("the availability alert still went out", "AVAILABLE" in subs)
    check_true("and so did the held-ticket alert",
               "HELD" in subs.upper() or "SECURED" in subs.upper())
finally:
    config.SECURE_ON_FIND, buyer.secure_in_thread = was_flag, was_secure

print("\nA page marked alerts-only is never handed to the buyer")
attempts.clear()
was_flag, was_secure = config.SECURE_ON_FIND, buyer.secure_in_thread
was_securable = early_event.secure
try:
    config.SECURE_ON_FIND = True
    buyer.secure_in_thread = fake_secure
    early_event.secure = False
    state = st.load()
    make_due(state)
    st.event_state(state, EARLY)["known_listings"] = []
    st.event_state(state, EARLY)["last_resale"] = UNAVAILABLE
    st.save(state)
    browser.answers = {EARLY: found}
    reading, mails = cycle(browser)
    check("the buyer was not called", attempts, [])
    check_true("but the alert still went out — watching is not securing",
               any("AVAILABLE" in (m["Subject"] or "") for m in mails))
finally:
    config.SECURE_ON_FIND, buyer.secure_in_thread = was_flag, was_secure
    early_event.secure = was_securable

print("\nState survives the cycle that wrote it")
state = st.load()
check_true("the state file was actually written", config.STATE_FILE.exists())
check_true("every page has its own history",
           all(slug in state.get("events", {}) for slug in SLUGS))
check_true("and its own next-gap draw, so the range is not re-rolled each tick",
           all(st.event_state(state, slug).get("next_gap_seconds")
               for slug in SLUGS))

_tmpdir.cleanup()
print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
