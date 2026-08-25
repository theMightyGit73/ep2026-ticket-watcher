"""Telling David and going back for the ticket are separate decisions.

They were one decision, on one clock, and it cost a real chance.

On 2026-08-20 the attempt at 20:02 lost the race. The resale sweep then saw
Weekend Camping stock on the page again at 20:04, and again at 20:06, and the
watcher did nothing on either sighting. Not because anything failed — because
securing was only reachable from inside the alerting branch of handle(), and
neither reading earned an alert: resale already read AVAILABLE so there was no
edge, the listing described identically to the one already known so nothing
looked new, and the four-minute re-nag had not elapsed. Four minutes of
visible stock against a single thirteen-second attempt.

The two questions are not the same question. A repeat email is noise David has
to ignore; a repeat attempt is the only job the machine has. So the alerting
clock stays exactly as it was — quiet, four-minutely, description-based — and
securing gets its own, much shorter one.

What must NOT change: the clock is a floor on how often the browser is sent at
a page, not a licence to ignore the things that genuinely mean "do not try".
A live hold still owns the browser, and its priority rules still decide.

Run with:  .venv/bin/python tests/test_secure_clock.py
"""

import smtplib
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config, engine, state as st  # noqa: E402
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


def body_of(msg):
    return msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


class FakeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): sent.append(msg)


smtplib.SMTP_SSL = FakeSMTP
# _send_email refuses before it reaches SMTP when these are unset, and
# run_tests.sh unsets them deliberately. The fake transport above is what
# actually stops anything leaving the machine.
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "test-password"

# The suite runs with the shipped defaults, and the shipped default for
# securing is OFF — run_tests.sh unsets EP_SECURE_ON_FIND deliberately, so a
# suite started from a shell that has sourced the env file cannot disagree
# with one started from a clean one. Everything below is about what happens
# when it IS on, so turn it on for the file.
config.SECURE_ON_FIND = True

SLUG = config.EVENTS[0].slug
DESCRIPTION = "Verified Resale — Section STNDN1 — €366.39"


def a_find(slug=SLUG):
    r = Reading(source="resale-sweep")
    r.event_slug = slug
    r.event_name = config.EVENTS[0].name
    r.event_url = config.EVENTS[0].url
    r.primary = UNAVAILABLE
    r.resale = AVAILABLE
    r.listings.append(
        Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                listing_id="lnew1", section="STNDN1"))
    return r


def already_alerted_state():
    """State as it stood at 20:04: stock known, David told two minutes ago.

    Every gate in should_alert_availability is deliberately satisfied in the
    "stay quiet" direction — resale already good, the listing already known by
    its description, the re-nag not yet elapsed. This is the exact shape that
    used to suppress the attempt along with the email.
    """
    state = dict(st._defaults())
    ev = st.event_state(state, SLUG)
    ev["last_resale"] = AVAILABLE
    ev["last_primary"] = UNAVAILABLE
    ev["known_listings"] = [DESCRIPTION]
    ev["last_availability_alert"] = (
        st.utc_now() - timedelta(minutes=2)).isoformat()
    return state


print("\nFirst, the thing that must still be true: this reading earns no email")
state = already_alerted_state()
should, reason = st.should_alert_availability(
    state, a_find(), st.pending_listings(state, a_find()))
check("no alert is due", should, False)
check("and no reason is offered", reason, "")


print("\nBut it does earn another attempt")
check_true("securing is allowed", st.should_try_again(state, a_find()))


print("\nThe gates that genuinely mean 'do not try'")
off = already_alerted_state()
try:
    config.SECURE_ON_FIND = False
    check("not when securing is switched off",
          st.should_try_again(off, a_find()), False)
finally:
    config.SECURE_ON_FIND = True

quiet = a_find()
quiet.resale = UNAVAILABLE
check("not when there is nothing to take",
      st.should_try_again(already_alerted_state(), quiet), False)

# A live hold owns the one buying browser. Preemption is real and this project
# does it, but it belongs to _maybe_secure and is driven by page priority —
# letting a retry clock reach past it would let a page preempt itself.
holding = already_alerted_state()
st.note_hold(holding, 10, event_slug=SLUG, priority=100)
check("not while a ticket is already held",
      st.should_try_again(holding, a_find()), False)


print("\nThe floor: one attempt a minute per page, not one per sighting")
state = already_alerted_state()
check_true("the first one is allowed", st.should_try_again(state, a_find()))
st.note_secure_attempt(state, SLUG)
check("and the next is not, straight away",
      st.should_try_again(state, a_find()), False)

# Stamped at the START of the attempt on purpose. Measuring from the finish
# would let a two-minute attempt be followed a second later by the next one,
# which is the opposite of a floor.
st.event_state(state, SLUG)["last_secure_attempt"] = (
    st.utc_now() - timedelta(seconds=config.SECURE_MIN_INTERVAL_SECONDS + 1)
).isoformat()
check_true("but it is once the interval has passed",
           st.should_try_again(state, a_find()))

# Per page, or one busy page would silence the other.
other = already_alerted_state()
st.note_secure_attempt(other, SLUG)
check_true("and the clock is per page",
           st.should_try_again(other, a_find(config.EVENTS[1].slug)))


print("\nEnd to end: a sighting that earns no email still sends the browser")
# The 20:04 case, driven through handle() rather than asserted about in
# pieces. This is the one that would have caught the original bug.
attempts = []


def fake_secure(event, listing, timeout_s=None, may_preempt=False, worker=None):
    attempts.append(event.slug)
    out = buyer.HoldResult()
    out.reason = "fixture: did not secure"
    return out


was_secure = buyer.secure_in_thread
was_browser = config.USE_BROWSER
try:
    config.USE_BROWSER = False          # no liveness beacon, no network probe
    buyer.secure_in_thread = fake_secure

    state = already_alerted_state()
    engine.handle(a_find(), state)
    check("the browser was sent anyway", attempts, [SLUG])
    check("and no email went with it", sent, [])
    check_true("the attempt is stamped for the next decision",
               st.event_state(state, SLUG)["last_secure_attempt"])

    # Immediately again: the alert clock still says no, and now the securing
    # clock does too. A sweep that sees the same stock every ninety seconds
    # must not queue up attempts behind a browser that is already busy.
    attempts.clear()
    engine.handle(a_find(), state)
    check("a second sighting inside the minute is left alone", attempts, [])

    # And once the minute is up it goes again, because the ticket is still
    # sitting there.
    st.event_state(state, SLUG)["last_secure_attempt"] = (
        st.utc_now() - timedelta(seconds=config.SECURE_MIN_INTERVAL_SECONDS + 1)
    ).isoformat()
    engine.handle(a_find(), state)
    check("and goes back once the floor has passed", attempts, [SLUG])
finally:
    config.USE_BROWSER = was_browser
    buyer.secure_in_thread = was_secure


print("\nA hold won on the retry path is still written down")
# Two routes reach a hold now, and a hold the second route forgot to record
# would be a live basket the watchdog throws away twenty minutes later. This
# is the failure that would be invisible until it was expensive.
def fake_hold(event, listing, timeout_s=None, may_preempt=False, worker=None):
    out = buyer.HoldResult()
    out.secured = True
    out.minutes_hint = 10
    return out


was_secure = buyer.secure_in_thread
was_browser = config.USE_BROWSER
try:
    config.USE_BROWSER = False
    buyer.secure_in_thread = fake_hold
    state = already_alerted_state()
    engine.handle(a_find(), state)
    check_true("the hold is recorded", st.hold_remaining(state) > 0)
    check("under the page that won it", state["hold_event_slug"], SLUG)
    check("with its priority, so a weekend ticket can still outrank it",
          state["hold_priority"], config.EVENTS[0].secure_priority)

    # The one standing instruction with a trigger rather than a date on it.
    # David switched the Early Entry Pass off on 2026-08-20 because the
    # weekend ticket was the critical thing and he did not have one, and asked
    # to be able to turn it back on easily once he did. That moment has just
    # happened, and it is the worst possible moment to expect anyone to
    # remember a config flag — there is a live basket with a countdown on it.
    was_early = config.WATCH_EARLY_ENTRY
    try:
        config.WATCH_EARLY_ENTRY = False
        sent.clear()
        engine.handle(a_find(), already_alerted_state())
        early = [m for m in sent if "Early Entry" in str(m.get("Subject", ""))]
        check("a weekend hold says the pass is worth having again",
              len(early), 1)
        body = body_of(early[0]) if early else ""
        check_true("naming the exact switch", "EP_EARLY_ENTRY=1" in body)
        check_true("and the restart that applies it", "restart.sh" in body)
        check_true("and saying to pay for the ticket first",
                   "Pay for the ticket first" in body)

        # Not sent when the pass is already on — there is nothing to turn on,
        # and a reminder about a setting already in force is pure noise at the
        # single most urgent moment this project has.
        config.WATCH_EARLY_ENTRY = True
        sent.clear()
        engine.handle(a_find(), already_alerted_state())
        check("and says nothing when it is already on",
              [m for m in sent if "Early Entry" in str(m.get("Subject", ""))], [])
    finally:
        config.WATCH_EARLY_ENTRY = was_early
finally:
    config.USE_BROWSER = was_browser
    buyer.secure_in_thread = was_secure


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)


# ── The submit timeout must outlast every path an attempt can take ───────
#
# Added 2026-08-24 after the change that caused the outage it now prevents.
#
# secure_budget_seconds() bounds the chase, and BuyerWorker.submit() waits on
# the job for budget + 60. Cutting the two chase ceilings from 720 to 120 that
# evening dropped the submit timeout to 180s — while a challenged attempt,
# which waits a block out on SECURE_CHALLENGE_* timings that neither ceiling
# bounds, was measured the day before at 271s and 281s.
#
# The first block of the night duly overran, the attempt was abandoned
# mid-flight, the worker stayed busy behind it, and six consecutive listings
# were reported as "already open holding something" while nothing was held.
#
# So: every path has to fit inside the number the worker waits on.
print("\nthe worker's timeout outlasts a challenged attempt")

budget = config.secure_budget_seconds()
submit_timeout = budget + 60          # secure_in_thread()'s arithmetic

# The two live measurements, plus room. A challenged attempt is the longest
# thing an attempt is ALLOWED to be; anything past this really is hung.
WORST_OBSERVED_CHALLENGE = 281

check("the submit timeout clears the worst measured challenge",
      submit_timeout > WORST_OBSERVED_CHALLENGE, True)
check("with real headroom, not a couple of seconds",
      submit_timeout - WORST_OBSERVED_CHALLENGE >= 60, True)

# The budget must actually account for the challenge path rather than happening
# to be large. If someone cuts the chase ceilings again, this has to move too.
check("the budget exceeds the chase ceilings on its own",
      budget > max(config.SECURE_TIMEOUT_SECONDS,
                   config.SECURE_ACTIVE_TIMEOUT_SECONDS), True)

# And it must not creep back up to the twelve-minute window that occupied the
# buying browser for a fifth of 2026-08-24.
check("but stays well under the old twelve-minute window",
      budget <= 420, True)
