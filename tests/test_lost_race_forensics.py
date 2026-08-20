"""When a hold fails, say WHY in a way that decides what to do next.

Two real weekend listings at €366.39 were found and lost on 2026-08-20, half
an hour apart, and both failed identically: the buying browser found the row
in 0.0s, clicked it, reached the listing's own page and was told the ticket
had been "sold or removed from sale". The attempts took 14 and 17 seconds.

Fourteen seconds sounds like a speed problem, and the temptation is to shave
it. But nothing in the record could separate the two explanations, and they
call for OPPOSITE responses:

  * SOLD — somebody genuinely bought it in those seconds. Then every second
    is worth chasing, and David should not bother refreshing.
  * HELD — it is in another buyer's basket, or the feed is advertising
    something the offer flow will not honour. Then speed wins nothing at all,
    because there was nothing to win; the useful move is to come back in a few
    minutes, because baskets expire.

Ticketmaster's own resale feed can tell them apart, and asking costs one XHR
from a page that is already open. If the ticket is still listed a second after
being refused, it did not sell.

That is what this file pins: the probe runs, its answer reaches the email in
words David can act on, and — the part that matters most — it NEVER converts a
failed hold into an exception. This runs on the one path where he is already
not getting a ticket.

Run with:  .venv/bin/python tests/test_lost_race_forensics.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config, notify  # noqa: E402
from ep_watcher.model import Listing  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


LISTING = Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                  listing_id="lmnlh641", section="STNDN1")
EVENT = config.EVENTS[0]


class FakePage:
    def __init__(self, url="https://www.ticketmaster.ie/dead-end"):
        self.url = url


class FakeSession:
    """Answers listings_now() with whatever the test wants the feed to say."""

    def __init__(self, record):
        self._record = record
        self.asked = 0

    def listings_now(self, event, qty):
        self.asked += 1
        if isinstance(self._record, Exception):
            raise self._record
        return self._record


def feed(*ids):
    return {"status": 200, "data": {
        "total": len(ids),
        "picks": [{"resaleListingId": i, "section": "STNDN1"} for i in ids],
    }}


def probe(record):
    result = buyer.HoldResult()
    session = FakeSession(record)
    buyer._probe_after_gone(session, EVENT, LISTING, result, FakePage())
    return result


print("\nStill in the feed after the refusal — it did not sell")

# The case that changes the advice. The id is the very one we tried, so there
# is no question of it being a different ticket.
r = probe(feed("lmnlh641"))
check("recorded as still listed", r.still_listed_after, True)
check("with the ids the feed returned", r.ids_after, ["lmnlh641"])
check("and the id we were after", r.listing_id, "lmnlh641")
check_true("the note says the feed still lists it",
           any("still lists" in n for n in r.notes))

# Same verdict when the feed returns a DIFFERENT id for the same ticket.
# These ids have been observed changing between polls for what is plainly one
# listing, so a mismatch is evidence about the feed, not proof of a new
# ticket — and either way something is still on sale.
r2 = probe(feed("lkp8v59y7s"))
check("a changed id is still 'listed'", r2.still_listed_after, True)
check_true("and the note says it is not the one we tried",
           any("NOT among them" in n for n in r2.notes))


print("\nEmpty feed after the refusal — it really did go")

r3 = probe(feed())
check("recorded as gone", r3.still_listed_after, False)
check_true("and the note agrees", any("really did go" in n for n in r3.notes))


print("\nThe probe can never make a bad outcome worse")

# Every one of these used to be a plain crash inside the failure path, which
# would have replaced an honest "could not hold it" email with a traceback.
for label, record in [
    ("the endpoint raising", RuntimeError("boom")),
    ("a null answer", None),
    ("a shapeless answer", {"status": 200, "data": None}),
    ("nonsense in data", {"status": 200, "data": {"picks": "not a list"}}),
]:
    try:
        out = probe(record)
        check(f"{label} is survived", True, True)
        check(f"{label} leaves the verdict unknown or false",
              out.still_listed_after in (None, False), True)
    except Exception as exc:
        check(f"{label} is survived", f"raised {type(exc).__name__}", True)

# And the dead end's URL is kept whatever the feed says, because it is the
# only place a direct link to one listing has been seen.
check("the landing URL is captured",
      probe(feed()).landed_url, "https://www.ticketmaster.ie/dead-end")


print("\nThe email tells David which of the two happened")

held = buyer.HoldResult()
held.still_listed_after = True
held.ids_after = ["lmnlh641"]
block = notify._verdict_block(held)
check_true("says it may not be gone", "MAY NOT ACTUALLY BE GONE" in block)
check_true("and tells him to try again", "TRY THE LINK AGAIN" in block)

sold = buyer.HoldResult()
sold.still_listed_after = False
block = notify._verdict_block(sold)
check_true("says it really went", "really did go" in block)
check_true("and that refreshing is pointless", "not bring it" in block)

# Unknown stays silent rather than guessing. A confident wrong answer here
# sends him either to a dead page or away from a live one.
check("an unasked question says nothing",
      notify._verdict_block(buyer.HoldResult()), "")


print("\nThe phone channel is inert until it is configured")

# Off is the default, and off must be quiet and harmless: this is fired from
# the availability alert, which is the one message that must always get out.
check("not configured by default", config.can_ring_phone(), False)
check("so ringing does nothing", notify.ring_phone("test"), False)

# ...and it must not raise even when half-configured, which is exactly the
# state a partly-copied env file leaves it in.
for missing in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "ALERT_PHONE"):
    was = {n: getattr(config, n) for n in
           ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "ALERT_PHONE")}
    try:
        for n in was:
            setattr(config, n, "set")
        setattr(config, missing, "")
        check(f"half-configured ({missing} absent) does not ring",
              notify.ring_phone("test"), False)
    finally:
        for n, v in was.items():
            setattr(config, n, v)

print("\nA number that cannot be dialled is caught before it matters")

# Every one of these is a mistake somebody actually makes copying a number off
# a phone, and every one of them fails as an HTTP error at the moment a ticket
# is on screen rather than as anything visible beforehand.
for bad, expect in [
    ("089 708 5212", "no country code"),
    ("0899999999", "no country code"),
    ("00353897085212", "dialling prefix"),
    ("+3530897085212", "keeps the 0"),
    ("+353 89 SEVEN", "not a digit"),
    ("+353", "outside the 8-15"),
    ("", "not set"),
]:
    problem = config.phone_problem(bad)
    check_true(f"{bad!r} is rejected", problem is not None)
    check_true(f"...and says why ({expect})", problem and expect in problem)

# The real one, in the form the env file now holds.
check("a correct Irish mobile passes",
      config.phone_problem("+353897085212"), None)
check("and so does a US number", config.phone_problem("+15551234567"), None)
# Whitespace either side is what a copy-paste leaves behind.
check("surrounding spaces are tolerated",
      config.phone_problem("  +353897085212  "), None)


print("\nGoing back for a ticket that did not sell")


class ScriptedSecure:
    """Stands in for _secure_once, returning a scripted outcome per attempt."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, session, event, listing, result=None, deadline=None):
        self.calls += 1
        secured, still = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        result = result or buyer.HoldResult()
        result.secured = secured
        result.still_listed_after = still
        return result


def run_secure(*outcomes, retries=2, pause=0.0):
    real_once, real_retries, real_pause = (
        buyer._secure_once, config.SECURE_RETRIES,
        config.SECURE_RETRY_PAUSE_SECONDS)
    scripted = ScriptedSecure(*outcomes)
    try:
        buyer._secure_once = scripted
        config.SECURE_RETRIES = retries
        config.SECURE_RETRY_PAUSE_SECONDS = pause
        out = buyer.secure(None, EVENT, LISTING)
        return out, scripted.calls
    finally:
        buyer._secure_once = real_once
        config.SECURE_RETRIES = real_retries
        config.SECURE_RETRY_PAUSE_SECONDS = real_pause


# Sold: the feed agreed it was gone. Going back would spend requests against a
# rate limit for a ticket that does not exist.
out, calls = run_secure((False, False))
check("a ticket that really sold is not chased", calls, 1)
check("and is reported as a failure", out.secured, False)

# Unknown: the probe could not ask. Treated like sold — a retry campaign
# started on a guess is a retry campaign against nothing.
out, calls = run_secure((False, None))
check("an unanswerable probe is not chased either", calls, 1)

# Still listed: this is the case worth waiting out.
out, calls = run_secure((False, True), retries=2)
check("a ticket still in the feed is retried to the limit", calls, 3)
check_true("and the notes say why it went back",
           any("did not sell" in n for n in out.notes))
check_true("and say when it gave up",
           any("is the limit" in n for n in out.notes))

# A retry that works stops immediately — no further attempts after a basket.
out, calls = run_secure((False, True), (True, None), retries=5)
check("a successful retry stops at once", calls, 2)
check("and reports the hold", out.secured, True)

# The retry count is honoured, including zero.
_, calls = run_secure((False, True), retries=0)
check("retries can be switched off entirely", calls, 1)
_, calls = run_secure((False, True), retries=1)
check("one retry means two attempts", calls, 2)

# The budget is the bound. With no time left for a pause, it must not sleep.
real_timeout = config.SECURE_TIMEOUT_SECONDS
try:
    config.SECURE_TIMEOUT_SECONDS = 0
    out, calls = run_secure((False, True), retries=5, pause=30.0)
    check("no time in the window means no going back", calls, 1)
    check_true("and it says so rather than going quiet",
               any("no time left" in n for n in out.notes))
finally:
    config.SECURE_TIMEOUT_SECONDS = real_timeout


print("\nThe quantity is set while the browser is idle, not on the clock")


class FakeSess:
    def __init__(self, blow_up=False):
        self.set_to = None
        self.blow_up = blow_up

    def set_quantity(self, qty, sink):
        if self.blow_up:
            raise RuntimeError("stepper missing")
        self.set_to = qty
        sink.note("quantity set")


worker = buyer.BuyerWorker.__new__(buyer.BuyerWorker)
worker._session = FakeSess()
worker._prearm()
check("parking sets the wanted quantity in advance",
      worker._session.set_to, config.WANTED_QUANTITY)

# A page that will not take a quantity here must not take out the warm
# browser — the attempt itself can deal with it.
worker._session = FakeSess(blow_up=True)
try:
    worker._prearm()
    check("a failure while pre-arming is swallowed", True, True)
except Exception as exc:
    check("a failure while pre-arming is swallowed",
          f"raised {type(exc).__name__}", True)

# The sink it passes must accept note() — set_quantity calls it.
buyer._ParkNotes().note("anything")
check("the parking note sink accepts notes", True, True)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
