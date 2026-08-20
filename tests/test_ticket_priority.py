"""The weekend ticket always wins the buying browser. The pass still gets tried.

David's rule, 2026-08-19: "weekend ticket is always priority, but try to get
the early ticket as well."

Both halves are load-bearing and they pull against each other. There is one
buying browser and one account, so two listings cannot be held at once — and
the Early Entry Pass appeared four times on 2026-08-19 against four weekend
sightings, so the collision is an ordinary afternoon rather than a corner
case. Before this rule existed the browser went to whichever listing was seen
first, and a €46.50 add-on could lock out a €366 ticket for the length of its
countdown.

The pass is only valid alongside a Weekend Ticket. So holding one while a
weekend ticket goes past is the worst outcome available: the single browser
spent on the single product that is useless on its own. Hence precedence, and
hence preemption — a weekend ticket closes the browser on a held pass.

That costs something real and the code says so: it drops a certain hold for
one that may already be gone. It is still the right way round.

Run with:  .venv/bin/python tests/test_ticket_priority.py
"""

import smtplib
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config, engine, notify, state as st  # noqa: E402
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
engine.network = type("_Net", (), {
    "public_ip": staticmethod(lambda *a, **kw: "10.0.0.1"),
    "fingerprint": staticmethod(lambda *a, **kw: {"key": "aa:bb", "ip": "10.0.0.1"}),
})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "x"
config.NTFY_TOPIC = None

WEEKEND = next(e for e in config.EVENTS if e.slug == "weekend-camping")
INSTALMENT = next(e for e in config.EVENTS if e.slug == "weekend-camping-instalment")
# A stand-in for "a securable page that ranks below a weekend ticket".
#
# This used to be the live Early Entry event, which broke the moment that page
# was set to alert-only on 2026-08-20 — the third time that setting has moved.
# What is under test here is the PRECEDENCE MACHINERY, which is independent of
# which pages happen to be securable this week, so the fixture is built rather
# than borrowed. If an add-on is ever made securable again it inherits exactly
# these rules, and this file will still be checking them.
EARLY = config.Event(
    slug="early-entry-fixture",
    name="Electric Picnic 2026 - Early Entry Pass",
    url=config.EVENTS[-1].url,
    secure=True,
    secure_priority=config.SECURE_PRIORITY_ADDON,
)
LIVE_EARLY = next(e for e in config.EVENTS if e.slug == "early-entry")


print("\nThe ranking itself")
check_true("a weekend ticket outranks the Early Entry pass",
           WEEKEND.secure_priority > EARLY.secure_priority)
check("both weekend pages rank equally — either is the real ticket",
      WEEKEND.secure_priority, INSTALMENT.secure_priority)
# The live pass is alert-only as of 2026-08-20 — it appears several times a
# day and each attempt spends a buying-browser cold start. Asserted here so
# that flipping it back is a deliberate act with a failing test behind it.
check("the live pass is watched, not secured", LIVE_EARLY.secure, False)
check_true("but it keeps its precedence, so re-enabling restores give-way",
           LIVE_EARLY.secure_priority < WEEKEND.secure_priority)


print("\nThe cycle spends its attention on the weekend page first")
# A cycle can end early — a 403 stops it dead — and whichever page went last
# is the one skipped. It must never be the weekend ticket.
ordered = sorted(list(config.EVENTS) + [EARLY], key=lambda e: -e.secure_priority)
check("the Early Entry pass is last in precedence", ordered[-1].slug, EARLY.slug)
check_true("and a weekend page is first", ordered[0].secure_priority == WEEKEND.secure_priority)


def a_find(event):
    r = Reading(source="browser", primary=UNAVAILABLE, resale=AVAILABLE)
    r.event_slug, r.event_name, r.event_url = event.slug, event.name, event.url
    r.listings.append(Listing(f"Verified Resale — {event.slug}", "€366.39",
                              "resale", listing_id="x1", section="STNDN1"))
    return r


def attempt(event, state, hold_to_return):
    """Run the securing decision for `event` against `state`.

    The stand-in for secure_in_thread honours the same contract the real one
    does: when the buying browser is occupied and this ticket has not been
    given permission to preempt, it refuses and holds nothing. Without that,
    the fake would report a hold the real code would never have taken, and
    the test would pass while the rule was broken.
    """
    seen = {}
    browser_busy = st.held_priority(state) > 0

    def fake_secure(ev, listing, timeout_s=None, may_preempt=False,
                    worker=None):
        seen["slug"] = ev.slug
        seen["may_preempt"] = may_preempt
        if browser_busy and not may_preempt:
            return buyer.HoldResult(
                secured=False,
                reason="the buying browser is already open holding something "
                       "at least as important as this",
            )
        hold_to_return.preempted = may_preempt
        return hold_to_return

    # A genuinely new sighting. Alerts are edge-triggered per page, so without
    # clearing the page's memory the second scenario for the same event would
    # be silently skipped and the test would prove nothing.
    ev_state = st.event_state(state, event.slug)
    ev_state["known_listings"] = []
    ev_state["last_resale"] = UNAVAILABLE
    ev_state["last_availability_alert"] = None

    was_flag, was_secure = config.SECURE_ON_FIND, buyer.secure_in_thread
    was_events = config.EVENTS
    try:
        config.SECURE_ON_FIND = True
        buyer.secure_in_thread = fake_secure
        # _maybe_secure resolves the reading's slug against config.EVENTS and
        # declines anything it cannot find, so a fixture page has to be
        # visible there for the duration. Registered rather than substituted,
        # so the real pages keep their real precedence alongside it.
        if event not in config.EVENTS:
            config.EVENTS = list(config.EVENTS) + [event]
        engine.handle(a_find(event), state)
    finally:
        config.SECURE_ON_FIND, buyer.secure_in_thread = was_flag, was_secure
        config.EVENTS = was_events
    return seen


print("\nWith nothing held, the pass is attempted — 'get the early one as well'")
state = dict(st._defaults())
seen = attempt(EARLY, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("the pass was attempted", seen.get("slug"), EARLY.slug)
check("with nothing to preempt", seen.get("may_preempt"), False)
check("and the hold is recorded against the pass", state["hold_event_slug"], EARLY.slug)
check("at the pass's own precedence", st.held_priority(state), EARLY.secure_priority)


print("\nA weekend ticket then takes the browser off the held pass")
sent.clear()
seen = attempt(WEEKEND, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("the weekend ticket was attempted", seen.get("slug"), WEEKEND.slug)
check_true("and it was allowed to preempt", seen.get("may_preempt"))
check("the recorded hold is now the weekend ticket",
      state["hold_event_slug"], WEEKEND.slug)
check("at weekend precedence", st.held_priority(state), WEEKEND.secure_priority)
body = "\n".join(m.get_payload()[0].get_payload(decode=True).decode("utf-8")
                 for m in sent)
check_true("and he is told the pass was let go", "LET GO" in body)
check_true("with the reason, so it does not read as a bug", "only valid alongside" in body)


print("\nBut the pass may NOT take the browser off a weekend ticket")
seen = attempt(EARLY, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("the pass is not permitted to preempt", seen.get("may_preempt"), False)
check("and the weekend hold is left exactly where it was",
      state["hold_event_slug"], WEEKEND.slug)


print("\nNor may one weekend page evict the other — they rank equally")
seen = attempt(INSTALMENT, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("equal precedence does not preempt", seen.get("may_preempt"), False)
check("the first weekend hold survives", state["hold_event_slug"], WEEKEND.slug)


print("\nA lapsed hold stops outranking anything")
# Otherwise a stale marker would block every find for the rest of the day.
state["hold_until"] = (st.utc_now() - timedelta(minutes=1)).isoformat()
check("an expired hold has no precedence", st.held_priority(state), 0)
seen = attempt(EARLY, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("so the pass is attempted again", seen.get("slug"), EARLY.slug)
check("without needing to preempt anything", seen.get("may_preempt"), False)


print("\nIf the swap fails, the record must not still claim a ticket is held")
# Dropping a certain hold for one that turns out to be gone is the cost of
# the rule. What must not also happen is state insisting something is held.
state = dict(st._defaults())
attempt(EARLY, state, buyer.HoldResult(secured=True, minutes_hint=11))
check("the pass is held", state["hold_event_slug"], EARLY.slug)
sent.clear()
missed = buyer.HoldResult(secured=False, reason="the listing had genuinely sold")
attempt(WEEKEND, state, missed)
check("after a failed preemption nothing is recorded as held",
      st.held_priority(state), 0)
check("and no page is named as holding one", state["hold_event_slug"], None)
body = "\n".join(m.get_payload()[0].get_payload(decode=True).decode("utf-8")
                 for m in sent)
check_true("the failure email admits the pass was given up for nothing",
           "LET GO" in body)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
