"""When one source dies and another covers for it.

This is the failure that was live in production on 2026-08-14 and that no
test caught, because every existing test asked the opposite question.

The shape of it: engine.merge() marked a reading failed only when EVERY
source failed. The Discovery API answers from anywhere and has no bot
detection, so once it was configured it effectively never failed — which
meant a browser that was 403-blocked for hours produced a merged reading with
failed=False. That reset consecutive_failures on every poll, so the watchdog
could never reach its threshold, and the hourly email reported "0 failed"
while the only source that can see a resale listing was walled.

From the log, the same 403 before and after Discovery was added:

    13 Aug 20:37  (browser only)  hourly report: 5 checks, 3 failed
    14 Aug 04:40  (+ discovery)   hourly report: 3 checks, 0 failed

So: a partial reading must keep the data it did get, alert on it normally,
and still count as unhealthy. Both halves matter — counting it as a failure
outright would suppress a find, which is the worse bug of the two.

Run with:  .venv/bin/python tests/test_partial_readings.py
"""

import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import (  # noqa: E402
    AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading,
)
from ep_watcher.sources import browser, discovery  # noqa: E402

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
# The engine asks for the public IP on every poll to spot a network switch.
# Left real, this suite would make live HTTP calls and depend on the internet.
engine.network = type("_FixedIP", (), {"public_ip": staticmethod(lambda *a, **kw: "10.0.0.1")})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"
config.NTFY_TOPIC = None


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


def run(reading, state):
    sent.clear()
    engine.handle(reading, state)
    return list(sent)


def blocked_browser():
    r = Reading(source="browser", failed=True, blocked=True)
    return r.note("HTTP 403 — this client is rate-limited")


def quiet_discovery():
    """What Discovery says on an ordinary poll: sold out, resale unknowable."""
    return Reading(source="discovery-api", primary=UNAVAILABLE, resale=UNKNOWN)


print("\nmerge() records who answered and who did not")

m = engine.merge([quiet_discovery(), blocked_browser()])
check("names the failed source", m.failed_sources, ["browser"])
check("names the answering source", m.answering_sources, ["discovery-api"])
check("is NOT a total failure", m.failed, False)
check("is degraded", m.degraded, True)
check("still reports being blocked", m.blocked, True)

m_all_good = engine.merge([quiet_discovery(), Reading(source="browser", primary=UNAVAILABLE)])
check("a clean poll is not degraded", m_all_good.degraded, False)

m_all_dead = engine.merge([Reading(source="a", failed=True), Reading(source="b", failed=True)])
check("total failure is failed, not degraded", (m_all_dead.failed, m_all_dead.degraded),
      (True, False))

print("\nA partial poll does not count as a clean one")

s = dict(st._defaults())
# Seeded per event, not globally: the global count became a derived value
# (the worst event's) when a second ticket page was added, so that a healthy
# page could no longer reset a broken page's streak.
st.event_state(s, "")["consecutive_failures"] = 2
st.record_success(s, quiet_discovery(), healthy=False)
check("the failure counter keeps climbing", s["consecutive_failures"], 3)

s = dict(st._defaults())
s["consecutive_failures"] = 2
st.record_success(s, quiet_discovery(), healthy=True)
check("a genuinely clean poll still clears it", s["consecutive_failures"], 0)

print("\nThe regression itself: a blocked browser must escalate")

state = dict(st._defaults())
mails = []
for _ in range(config.WATCHDOG_FAILURE_THRESHOLD):
    mails = run(engine.merge([quiet_discovery(), blocked_browser()]), state)

check("four partial polls reach the watchdog", len(mails), 1)
body = body_of(mails[0])
check_true("the email names the browser as the failure", "browser" in body.lower())
check_true("and explains what is lost with it", "resale" in body.lower())
check("the failure counter was never reset", state["consecutive_failures"],
      config.WATCHDOG_FAILURE_THRESHOLD)

print("\n...and the hourly email must not call that hour healthy")

check("every partial poll counted as unhealthy",
      state["failures_since_heartbeat"], config.WATCHDOG_FAILURE_THRESHOLD)
check("and was counted as partial", state["degraded_since_heartbeat"],
      config.WATCHDOG_FAILURE_THRESHOLD)
check("and as resale-blind", state["resale_blind_since_heartbeat"],
      config.WATCHDOG_FAILURE_THRESHOLD)

sent.clear()
notify.heartbeat(
    checks=4, failures=4, hours=1.0,
    reading=Reading(source="t", primary=UNAVAILABLE, resale=UNKNOWN),
    coverage=(4, 4),
)
body = body_of(sent[-1])
check_true("the hour is reported as unhealthy", "EVERY check was unhealthy" in body)
check_true("resale blindness is spelled out", "RESALE WAS UNREADABLE" in body)
check_true("partial polls are itemised", "partial (a source failed)" in body)
# A bare "UNKNOWN" in the status column reads like a third flavour of "no".
check_true("UNKNOWN is explained, not left to look calm",
           "nothing could read this market" in body)

sent.clear()
notify.heartbeat(
    checks=12, failures=0, hours=1.0,
    reading=Reading(source="t", primary=UNAVAILABLE, resale=UNAVAILABLE),
    coverage=(0, 0),
)
body = body_of(sent[-1])
check_true("a real 'no' is left alone", "UNAVAILABLE" in body)
check_true("and a clean hour raises nothing",
           "RESALE WAS UNREADABLE" not in body and "nothing could read" not in body)

print("\nBut a find during a partial poll must still get through")

state = dict(st._defaults())
found = engine.merge([
    Reading(source="discovery-api", primary=AVAILABLE, resale=UNKNOWN,
            listings=[Listing("Electric Picnic 2026 - Weekend Camping", None, "primary")]),
    blocked_browser(),
])
check("the find survives the merge", found.primary, AVAILABLE)
check("and the poll is still marked degraded", found.degraded, True)
mails = run(found, state)
check("it alerts anyway", len(mails), 1)
check_true("with the buy link", config.EVENT_URL in body_of(mails[0]))

print("\nDiscovery must not claim a resale answer it cannot have")

# Exercised through the real check(), with only the HTTP calls stubbed out.
discovery.search_events = lambda *a, **kw: [
    {"id": "1", "name": "Electric Picnic 2026 - Campervan/Caravan Pass",
     "date": "2026-08-28", "status": "onsale", "price": None, "url": ""},
]
discovery.find_resale_events = lambda *a, **kw: []
discovery.configured = lambda: True
r = discovery.check()
check("no tmr events means UNKNOWN, not UNAVAILABLE", r.resale, UNKNOWN)
check("primary absence is still a real answer", r.primary, UNAVAILABLE)
check_true("and it says why", "do not know" in " ".join(r.notes))

print("\n...so it can never outrank a real reading, nor invent one")

merged = engine.merge([
    Reading(source="discovery-api", primary=UNAVAILABLE, resale=UNKNOWN),
    Reading(source="browser", primary=UNAVAILABLE, resale=UNKNOWN),
])
check("two 'do not know's stay unknown", merged.resale, UNKNOWN)
check("and are never good news", merged.any_good, False)

merged = engine.merge([
    Reading(source="discovery-api", primary=UNAVAILABLE, resale=UNKNOWN),
    Reading(source="browser", primary=UNAVAILABLE, resale=AVAILABLE),
])
check("a real find still wins", merged.resale, AVAILABLE)

print("\nA search that learned nothing is a failed read, not a quiet 'no'")


class FakeSession(browser.BrowserSession):
    """A real BrowserSession with only the Chrome-touching parts replaced.

    Subclassed rather than duck-typed so the parsing and the verdict under
    test are the genuine methods, not a paraphrase of them.
    """

    def __init__(self, outcome, page_text):
        self.outcome, self.page_text, self.headless = outcome, page_text, False

    def _load(self, reading):
        return browser._normalise(self.page_text)

    def _set_quantity(self, qty, reading):
        pass

    def _press_search(self, reading, qty):
        return True

    def _await_result(self, timeout_s=45):
        return self.outcome

    def _await_resale_panel(self, timeout_s=25, render_s=8.0, settle_s=2.0):
        # (readable, why) — the same contract the real method has, so a
        # future change to it breaks here rather than in production.
        if browser.RESALE_HEADING in browser._normalise(self.page_text):
            return True, "panel rendered"
        return False, "no resale call in this fixture"

    def visible_text(self):
        return self.page_text


def sweep(outcome, text):
    session = FakeSession(outcome, text)
    return session._search_quantities(Reading(source="browser"), browser._normalise(text))


nothing_learned = sweep("timeout", "Electric Picnic 2026\nFind Tickets")
check("both markets unknown", (nothing_learned.primary, nothing_learned.resale),
      (UNKNOWN, UNKNOWN))
check("so the read is marked failed", nothing_learned.failed, True)
check("and never reads as 'no tickets'", nothing_learned.any_good, False)

real_no = sweep(
    "rejected",
    "There aren't enough tickets to complete your request\nSearch Again\n"
    "Other Options\nVerified Resale Tickets\n"
    "Resale Tickets will appear below when they are available.",
)
check("a genuine refusal is a real answer", real_no.primary, UNAVAILABLE)
check("with an empty panel read as a real no", real_no.resale, UNAVAILABLE)
check("and is not marked failed", real_no.failed, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
