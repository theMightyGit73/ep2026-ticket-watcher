#!/usr/bin/env python3
"""Ticketmaster's refusal page carries its own verdict. Read it.

The `ctx` parameter on secure.ticketmaster.ie/error/q404 is a gzipped,
base64'd JSON document containing the listing Ticketmaster has just refused —
including `active`, which says whether the listing still exists.

That field settles the question the whole project has been guessing at. Every
refusal captured between 2026-08-21 and 2026-08-24 carries `"active": true`,
including the attempt of 2026-08-24 10:07 that clicked 5.3 seconds after the
sweep saw the listing. Those tickets had not sold, so "the race being lost at
the last step" was the wrong verdict on them, and the retry that gave up after
two goes gave up on live tickets.

These tests pin the decoder, and — more importantly — pin the BEHAVIOUR that
depends on it, because a decoder nobody acts on is what the last fortnight
already had.
"""

import base64
import gzip
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _sandbox  # noqa: F401  (isolates state, profiles and diagnostics)

from ep_watcher import buyer, config

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        FAILURES.append(label)


def truthy(label, got):
    ok = bool(got)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {got!r}")
        FAILURES.append(label)


def make_ctx(listing: dict, event_id: str = "18006314BD813D3E") -> str:
    """Build a ctx blob the way Ticketmaster does, so the test is not circular."""
    payload = json.dumps({
        "event": {"eventId": event_id, "name": "Electric Picnic 2026 - Weekend Camping"},
        "listing": listing,
    }).encode("utf-8")
    return urllib.parse.quote(base64.b64encode(gzip.compress(payload)).decode())


#: The exact listing payload decoded from
#: hold-20260824-110748-677-weekend-camping-try1.json — the 5.3-second attempt.
REAL_LISTING = {
    "id": 41966773,
    "urlId": "lw09yvzt",
    "eventId": "18006314BD813D3E",
    "active": True,
    "merchantAccount": "TicketmasterIE_Resale",
    "currency": "EUR",
    "facePrice": 300,
    "sellPrice": 310.5,
    "handlingFee": 0,
    "buyerFeeValue": 55.89,
    "isGeneralAdmission": True,
    "offerType": "Three+ Presale Ticket",
    "section": "STNDNG",
    "row": "GA6",
}

REAL_URL = ("https://secure.ticketmaster.ie/error/q404"
            "?cid=f6f41195-f209-409b-9782-ff9928164689"
            f"&ctx={make_ctx(REAL_LISTING)}")


print("\nread_error_context")
ctx = buyer.read_error_context(REAL_URL)
truthy("decodes a real refusal URL", ctx)
check("carries the listing", ctx.get("listing", {}).get("urlId"), "lw09yvzt")
check("reports active", ctx.get("listing", {}).get("active"), True)
check("reports the offer type",
      ctx.get("listing", {}).get("offerType"), "Three+ Presale Ticket")

print("\nread_error_context refuses to raise")
check("no ctx parameter", buyer.read_error_context(
    "https://www.ticketmaster.ie/event/18006314BD813D3E"), {})
check("empty url", buyer.read_error_context(""), {})
check("ctx that is not base64", buyer.read_error_context(
    "https://secure.ticketmaster.ie/error/q404?ctx=not-a-real-blob"), {})
check("ctx that is base64 but not gzip", buyer.read_error_context(
    "https://secure.ticketmaster.ie/error/q404?ctx="
    + urllib.parse.quote(base64.b64encode(b"plain text").decode())), {})
check("ctx that gunzips to something that is not a dict",
      buyer.read_error_context(
          "https://secure.ticketmaster.ie/error/q404?ctx="
          + urllib.parse.quote(base64.b64encode(
              gzip.compress(b'["a list"]')).decode())), {})

print("\ndescribe_offer")
check("names the type, seat and price",
      buyer.describe_offer(REAL_LISTING),
      "Three+ Presale Ticket · section STNDNG row GA6 · "
      "€310.50 (€366.39 with fees)")
check("empty listing says nothing", buyer.describe_offer({}), "")
check("a listing with no price still describes itself",
      buyer.describe_offer({"offerType": "GA", "section": "STNDNG"}),
      "GA · section STNDNG")


# ── The behaviour that matters ───────────────────────────────────────────────
#
# A decoder is only worth having if the retry acts on it. These drive secure()
# with a fake _secure_once, so the loop's decisions are tested without a
# browser: the point under test is which refusals it comes back for.

class FakeEvent:
    slug = "weekend-camping"
    name = "Electric Picnic 2026 - Weekend Camping"
    url = "https://www.ticketmaster.ie/x/event/18006314BD813D3E"


class FakeListing:
    listing_id = "lw09yvzt"


def run_secure(outcomes, monkey_sleep=True):
    """Drive secure() with a scripted sequence of attempt outcomes.

    `outcomes` is a list of dicts applied to the shared HoldResult in turn.
    Returns (result, number_of_attempts_made).
    """
    calls = {"n": 0}

    def fake_once(session, event, listing, result=None, deadline=None):
        i = calls["n"]
        calls["n"] += 1
        step = outcomes[min(i, len(outcomes) - 1)]
        for key, value in step.items():
            setattr(result, key, value)
        return result

    real_once, real_sleep = buyer._secure_once, buyer.time.sleep
    buyer._secure_once = fake_once
    if monkey_sleep:
        buyer.time.sleep = lambda s: None
    try:
        out = buyer.secure(None, FakeEvent(), FakeListing())
    finally:
        buyer._secure_once = real_once
        buyer.time.sleep = real_sleep
    return out, calls["n"]


print("\nsecure() comes back for a listing Ticketmaster calls active")
# The feed cannot be asked from the dead end (still_listed_after None) — the
# ordinary case, and the one that used to end the chase after a single go.
out, n = run_secure([{"listing_active": True, "still_listed_after": None}])
truthy(f"retries on active alone (made {n} attempts)", n > 1)
check("stops at the active-listing limit", n, config.SECURE_ACTIVE_RETRIES + 1)
truthy("remembers that it was ever active", out.ever_active)

print("\nsecure() still gives up when the listing is genuinely gone")
out, n = run_secure([{"listing_active": False, "still_listed_after": False}])
check("one attempt only", n, 1)
truthy("no active memory", not out.ever_active)

print("\nsecure() gives up when nothing can be established")
out, n = run_secure([{"listing_active": None, "still_listed_after": None}])
check("one attempt only", n, 1)

print("\nsecure() stops the moment it succeeds")
out, n = run_secure([
    {"listing_active": True, "still_listed_after": None},
    {"secured": True},
])
check("two attempts, then done", n, 2)
truthy("reports secured", out.secured)

print("\nthe verdict says what actually happened")
out, n = run_secure([{"listing_active": True, "still_listed_after": None}])
# The wording moved on 2026-08-24 from "this was never a race" to "this was
# not a race lost at the click", because the message stopped naming a cause it
# could not evidence. Either phrasing satisfies what is actually being
# defended: the reason must not blame speed for a refusal that arrived in a
# fifth of a second on a listing Ticketmaster called live.
truthy("does not call it a lost race", "race" not in out.reason.lower()
       or "never a race" in out.reason.lower()
       or "not a race" in out.reason.lower())
truthy("says the listing was active", "ACTIVE" in out.reason)

print("\nan active listing must not be reported as sold")
# The exact regression: attempt one sees it listed, attempt two does not, and
# the old code concluded "they had paid for it" while the error payload said
# the listing was live.
out, n = run_secure([
    {"listing_active": True, "still_listed_after": True},
    {"listing_active": True, "still_listed_after": False},
])
truthy("never claims it sold", "sold" not in out.reason.lower()
       or "had not sold" in out.reason.lower())

print("\nbudget arithmetic")
truthy("the worker's budget covers the longest chase",
       config.secure_budget_seconds() >= config.SECURE_ACTIVE_TIMEOUT_SECONDS)
truthy("and covers an ordinary one",
       config.secure_budget_seconds() >= config.SECURE_TIMEOUT_SECONDS)



# ── Waiting out a basket without hammering the search ────────────────────────
#
# The chase is only safe if the pause is cheap. Eleven full attempts in twelve
# minutes is ~55 searches/hour against a budget of 16.7, and the block screen
# that rate invites already causes half of all refusals. These pin that the
# waiting happens on the feed.

class FakePage:
    def __init__(self, url):
        self.url = url
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url


class FakeSession:
    """A buying session whose feed answers from a script."""

    def __init__(self, answers, url="https://secure.ticketmaster.ie/error/q404"):
        self._answers = list(answers)
        self.asked = 0
        self.page = FakePage(url)

    def listings_now(self, event, qty):
        self.asked += 1
        if not self._answers:
            return {"data": {"picks": []}}
        return self._answers.pop(0)


def ids_payload(*ids):
    return {"data": {"picks": [{"resaleListingId": i} for i in ids]}}


print("\n_wait_for_relist watches the feed instead of sleeping blind")
slept = []
real_sleep = buyer.time.sleep
buyer.time.sleep = lambda s: slept.append(s)
try:
    res = buyer.HoldResult()
    sess = FakeSession([ids_payload(), ids_payload("lw09yvzt")])
    came_back = buyer._wait_for_relist(
        sess, FakeEvent(), FakeListing(), res, 40.0,
        buyer.time.monotonic() + 600)
    truthy("returns True when the listing reappears", came_back)
    check("asked the feed twice", sess.asked, 2)
    truthy("navigated off the error page first",
           sess.page.gotos and "event" in sess.page.gotos[0])
    truthy("slept in short hops, not one long one",
           all(s <= config.SECURE_RELIST_POLL_SECONDS + 0.01 for s in slept))

    print("\n_wait_for_relist gives up quietly when it never comes back")
    slept.clear()
    res = buyer.HoldResult()
    sess = FakeSession([ids_payload()] * 20)
    came_back = buyer._wait_for_relist(
        sess, FakeEvent(), FakeListing(), res, 40.0,
        buyer.time.monotonic() + 600)
    truthy("returns False", not came_back)
    truthy("but did keep checking", sess.asked > 1)

    print("\n_wait_for_relist survives a session it cannot use")
    slept.clear()
    res = buyer.HoldResult()
    check("no session at all", buyer._wait_for_relist(
        None, FakeEvent(), FakeListing(), res, 5.0,
        buyer.time.monotonic() + 600), False)
    truthy("fell back to sleeping", slept)

    print("\n_wait_for_relist never runs past the overall deadline")
    slept.clear()
    res = buyer.HoldResult()
    sess = FakeSession([ids_payload()] * 50)
    past = buyer.time.monotonic() - 1
    check("a deadline already gone stops it at once",
          buyer._wait_for_relist(sess, FakeEvent(), FakeListing(),
                                 res, 300.0, past), False)
    check("and it asked nothing", sess.asked, 0)
finally:
    buyer.time.sleep = real_sleep

print("\nthe chase does not spend a search per pause")
truthy("feed checks are cheaper than the retry pause",
       config.SECURE_RELIST_POLL_SECONDS < config.SECURE_RETRY_PAUSE_SECONDS)


# ── The chase must survive the listing going invisible ───────────────────────
#
# The first live chases, 2026-08-24 12:21 and 12:28, stopped after six goes
# and four instead of ten. A ticket in somebody's basket is absent from the
# resale feed BY DEFINITION, so every attempt after the opening refusal finds
# no row, never clicks, never reaches an error page — and used to be judged on
# an empty feed as "it sold". That is the one state the chase exists for.

print("\nthe chase survives the listing going invisible mid-chase")
out, n = run_secure([
    # Opening refusal: Ticketmaster says the listing is live.
    {"listing_active": True, "still_listed_after": None},
    # Every look after that sees nothing at all — the basket hides it.
    {"listing_active": None, "still_listed_after": False},
])
check("keeps going to the active limit", n, config.SECURE_ACTIVE_RETRIES + 1)
truthy("and still remembers why", out.ever_active)
truthy("and never calls it sold", "sold" not in out.reason.lower()
       or "had not sold" in out.reason.lower())

print("\nbut an unproven listing that vanishes still ends at once")
out, n = run_secure([{"listing_active": None, "still_listed_after": False}])
check("one attempt only", n, 1)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}\n")
    sys.exit(1)
print("All checks passed.\n")
