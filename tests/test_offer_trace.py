#!/usr/bin/env python3
"""Why no ticket has ever been held: we were asking for none.

OfferTrace was built to settle a question. Ten distinct listings had been
refused, every one carrying `"active": true` in Ticketmaster's own error
payload, one of them clicked 5.3 seconds after the sweep saw it — which ruled
out speed and left two explanations that looked identical from outside: the
ticket is in somebody else's basket, or this client may not make an offer at
all. Rather than guess, it recorded the traffic the attempt was already
making.

It answered on the first listing, and the answer was neither. On 2026-08-24,
across four listings and eighteen requests without exception, the page asked:

    GET https://secure.ticketmaster.ie/{eventId}/{listingId}?qty=0
        -> 302 -> /error/q404

Zero tickets. Ticketmaster redirects a zero-quantity offer to the same "sold
or removed" screen a genuinely gone listing produces, and every securing
attempt this project has ever made has been refused for that and recorded as a
lost race. The listings were `active: true` because nothing was ever wrong
with them.

So this file now covers both halves: the trace that found it, and offer_url(),
which builds the same request with the quantity we actually want. The tests
that must never be got wrong are the qty ones — a zero rebuilds the bug — and
the redaction ones, because this browser carries a live Ticketmaster session
and a trace written to disk must never be a file that lets its reader become
David.

Run with:  .venv/bin/python tests/test_offer_trace.py
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _sandbox  # noqa: F401  (isolates state, profiles and diagnostics)

from ep_watcher import buyer, config
from ep_watcher.model import Listing
from ep_watcher.sources.browser import _listing_from_pick


class FakeEvent:
    """The real Weekend Camping page, for its event id."""

    slug = "weekend-camping"
    url = ("https://www.ticketmaster.ie/electric-picnic-2026-weekend-"
           "camping-co-laois-28-08-2026/event/18006314BD813D3E")

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


def falsy(label, got):
    ok = not got
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {got!r}")
        FAILURES.append(label)


# ── The offer id is derivable ────────────────────────────────────────────────
#
# Both observed offerIds base32-decode to `9|{resaleListingId}`. That makes the
# offer identity available the moment the feed answers, twenty seconds before
# the search path reaches the same fact.

print("\nofferIds are base32 of 9|{resaleListingId}")
# Both real offerIds captured, from finds five days apart.
for offer_id, listing_id in (("HF6GYMRXOQ2GQMTE", "l27t4h2d"),
                             ("HF6GYMDXG44WUYZQMZWA", "l0w79jc0fl")):
    check(f"{offer_id} decodes", buyer.decode_offer_id(offer_id), f"9|{listing_id}")
    check(f"and {listing_id} derives it back", buyer.offer_id_for(listing_id),
          offer_id)

print("\nthe derivation is total and never raises")
check("junk decodes to nothing", buyer.decode_offer_id("not base32 !!"), "")
check("empty decodes to nothing", buyer.decode_offer_id(""), "")
check("empty derives nothing", buyer.offer_id_for(""), "")
truthy("a round trip holds for an id never seen",
       buyer.decode_offer_id(buyer.offer_id_for("lzzz9new")) == "9|lzzz9new")

print("\nand the feed's offerIds are carried onto the listing")
# The exact pick captured on 2026-08-23.
pick = {
    "id": "l0w79jc0fl", "type": "general-seating", "section": "STNDN2",
    "originalPrice": 366.39, "description": "WEEKEND CAMPING", "areaName": "GA",
    "placeDescriptionId": "IE5DCLBTFQ2CYNI", "hasSpecialDescription": False,
    "offerIds": ["HF6GYMDXG44WUYZQMZWA"], "quality": 0.964912,
    "sellerBusinessType": "private", "resaleListingId": "l0w79jc0fl",
    "sellerAffiliationType": "unaffiliated", "attributes": [],
}
listing = _listing_from_pick(pick)
check("offer_ids survives the parse", listing.offer_ids,
      ("HF6GYMDXG44WUYZQMZWA",))
check("and the listing id still does too", listing.listing_id, "l0w79jc0fl")
check("a pick with no offerIds is still a listing",
      _listing_from_pick({"id": "x"}).offer_ids, ())
check("and a pick that is not a dict does not explode",
      _listing_from_pick("nonsense").offer_ids, ())
check("nor does a malformed offerIds field",
      _listing_from_pick({"id": "x", "offerIds": "not-a-list"}).offer_ids, ())


# ── Redaction: the part that must not be got wrong ───────────────────────────

print("\n_redact_url keeps the shape and drops the credentials")
dirty = ("https://secure.ticketmaster.ie/checkout"
         "?offerId=HF6GYM&session_token=abcdef123456&csrf=zzz&qty=1")
clean = buyer._redact_url(dirty)
truthy("the endpoint survives", "secure.ticketmaster.ie/checkout" in clean)
truthy("harmless parameters survive", "offerId=HF6GYM" in clean and "qty=1" in clean)
falsy("the token does not", "abcdef123456" in clean)
falsy("nor the csrf value", "zzz" in clean)
truthy("and the redaction says how long it was", "redacted" in clean)

print("\nevery credential-shaped name is caught")
for name in ("token", "authToken", "SESSIONID", "api_key", "password",
             "signature", "jwt", "bearer", "x-csrf", "cvv", "cardNumber",
             "otp", "secret", "nonce"):
    url = f"https://www.ticketmaster.ie/x?{name}=SUPERSECRETVALUE&keep=1"
    out = buyer._redact_url(url)
    if "SUPERSECRETVALUE" in out:
        print(f"  FAIL  {name} leaked")
        FAILURES.append(f"{name} leaked")
truthy("no credential-shaped parameter leaked", True)

print("\nlong blobs are truncated rather than written whole")
blob = "A" * 5000
out = buyer._redact_url(f"https://secure.ticketmaster.ie/error/q404?ctx={blob}")
truthy("the head is kept so the shape is recognisable", "AAAA" in out)
truthy("but not the whole thing", len(out) < 1000)

print("\n_redact_url never raises")
for bad in ("", "not a url at all", "http://[", "://///"):
    buyer._redact_url(bad)
truthy("survived every malformed url", True)


# ── The trace itself ─────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, url, method="GET", resource_type="document"):
        self.url = url
        self.method = method
        self.resource_type = resource_type


class FakeResponse:
    def __init__(self, url, status, body="", resource_type="document"):
        self.url = url
        self.status = status
        self._body = body
        self.request = FakeRequest(url, resource_type=resource_type)

    def text(self):
        return self._body


print("\nOfferTrace records the interesting traffic and ignores the rest")
t = buyer.OfferTrace()
t._on_request(FakeRequest("https://www.ticketmaster.ie/api/quickpicks/X/resale",
                          "GET", "xhr"))
t._on_request(FakeRequest("https://www.google-analytics.com/collect", "POST", "xhr"))
t._on_request(FakeRequest("https://www.ticketmaster.ie/logo.png", "GET", "image"))
t._on_response(FakeResponse("https://secure.ticketmaster.ie/error/q404", 404,
                            '{"error":"LISTING_NOT_AVAILABLE"}'))
t._on_response(FakeResponse("https://www.ticketmaster.ie/style.css", 200,
                            resource_type="stylesheet"))
entries = t.summary()
urls = " ".join(e.get("url", "") for e in entries)
truthy("the resale XHR is recorded", "quickpicks" in urls)
falsy("analytics is not", "google-analytics" in urls)
falsy("images are not", "logo.png" in urls)
falsy("a successful stylesheet is not", "style.css" in urls)
truthy("the failure is recorded", any(e.get("status") == 404 for e in entries))
truthy("with its body, which is where the real error code lives",
       any("LISTING_NOT_AVAILABLE" in (e.get("body") or "") for e in entries))

print("\nOfferTrace is bounded")
t = buyer.OfferTrace()
for i in range(500):
    t._on_request(FakeRequest(f"https://www.ticketmaster.ie/x/{i}", "POST", "xhr"))
check("it stops at the limit", len(t.summary()), buyer.OfferTrace.LIMIT)

print("\nOfferTrace never raises, whatever it is handed")
t = buyer.OfferTrace()
for junk in (None, object(), FakeRequest(None)):
    t._on_request(junk)
    t._on_response(junk)
    t._on_nav(junk)
truthy("survived every malformed event", True)

print("\nattach and detach survive a page that refuses both")
class HostilePage:
    def on(self, *a):
        raise RuntimeError("no listeners for you")

    def remove_listener(self, *a):
        raise RuntimeError("nor removal")

t = buyer.OfferTrace()
t.attach(HostilePage())
t.detach()
truthy("a broken trace does not cost the attempt", True)

print("\nthe result carries the new fields")
r = buyer.HoldResult()
check("trace starts empty", r.trace, [])
check("row_href starts empty", r.row_href, "")
check("offer_ids start empty", r.offer_ids, ())


# ── The offer URL, and the qty=0 bug it exists to fix ────────────────────────
#
# The trace of 2026-08-24 caught the page requesting, on four separate
# listings and eighteen times without exception:
#
#     GET https://secure.ticketmaster.ie/{eventId}/{listingId}?qty=0
#         -> 302 -> /error/q404
#
# Zero. Every securing attempt this project has ever made asked Ticketmaster
# for no tickets and was refused for it, and the refusal was recorded as
# somebody else having taken the listing. That retires both standing theories:
# the listings were always "active": true because nothing was wrong with them,
# and the 5.3-second attempt failed like all the rest because speed cannot
# rescue a malformed request.

print("\noffer_url builds what the page builds, with the quantity we want")
url = buyer.offer_url(FakeEvent(), "lp62f5thgs")
check("the observed format, exactly",
      url, "https://secure.ticketmaster.ie/18006314BD813D3E/lp62f5thgs?qty=1")

print("\nthe quantity is never zero — that is the whole bug")
falsy("no qty=0 by default", "qty=0" in buyer.offer_url(FakeEvent(), "lp62f5thgs"))
check("an explicit zero refuses to build a URL at all",
      buyer.offer_url(FakeEvent(), "lp62f5thgs", qty=0), "")
check("and so does a negative one",
      buyer.offer_url(FakeEvent(), "lp62f5thgs", qty=-1), "")
truthy("the default follows WANTED_QUANTITY",
       f"qty={config.WANTED_QUANTITY}" in buyer.offer_url(FakeEvent(), "lp62f5thgs"))
check("WANTED_QUANTITY is still one", config.WANTED_QUANTITY, 1)

print("\nit refuses rather than guesses when it lacks a part")
check("no listing id", buyer.offer_url(FakeEvent(), ""), "")
check("no listing id, none invented", buyer.offer_url(FakeEvent(), None), "")


class NoIdEvent:
    url = "https://www.ticketmaster.ie/some-page-with-no-event-id"


check("no event id in the url", buyer.offer_url(NoIdEvent(), "lp62f5thgs"), "")


class BustedEvent:
    url = None


check("no url at all", buyer.offer_url(BustedEvent(), "lp62f5thgs"), "")

print("\nit follows the event's own country rather than crossing borders")
class UKEvent:
    url = "https://www.ticketmaster.co.uk/x/event/18006314BD813D3E"


truthy("a .co.uk event goes to the .co.uk checkout",
       buyer.offer_url(UKEvent(), "lp62f5thgs").startswith(
           "https://secure.ticketmaster.co.uk/"))
truthy("an .ie event stays on .ie",
       buyer.offer_url(FakeEvent(), "lp62f5thgs").startswith(
           "https://secure.ticketmaster.ie/"))

print("\nthe URL agrees with what the feed already told us")
# The listing segment must equal the offerId's own listing id, which is what
# the eighteen traced requests showed in every case.
for offer_id in ("HF6GYMRXOQ2GQMTE", "HF6GYMDXG44WUYZQMZWA"):
    listing_id = buyer.decode_offer_id(offer_id).split("|", 1)[1]
    truthy(f"{offer_id} points at its own listing",
           f"/{listing_id}?" in buyer.offer_url(FakeEvent(), listing_id))



# ── The attempt must actually take that path ─────────────────────────────────
#
# A correct URL builder that nothing calls is what the last fortnight already
# had: `offerIds` sat parsed-and-discarded in the feed the whole time. These
# drive _secure_once against a fake page and assert on the URL it navigates to.

class DirectPage:
    """Records where it is sent, and reports whatever body it is given."""

    def __init__(self, body="", url="https://www.ticketmaster.ie/x/event/18006314BD813D3E"):
        self.url = url
        self.gotos = []
        self.body = body
        self.searched = False

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url

    def inner_text(self, _sel="body"):
        return self.body

    def title(self):
        return ""

    def get_by_role(self, *a, **k):
        self.searched = True
        raise AssertionError("the direct path must not press search")

    def get_by_text(self, *a, **k):
        raise AssertionError("the direct path must not hunt for a row")


class DirectSession:
    def __init__(self, page):
        self._page = page

    @property
    def page(self):
        return self._page

    def set_quantity(self, qty, result):
        raise AssertionError("the direct path must not touch the stepper")

    def await_listings(self, result, budget_s):
        raise AssertionError("the direct path must not wait for the panel")

    def listings_now(self, event, qty):
        return None


class RealListing:
    listing_id = "lp62f5thgs"
    offer_ids = ("HF6GY4BWGJTDK5DIM5ZQ",)
    section = "STNDNG"


buyer.session_evidence = lambda *a, **k: {"signed_in": True, "reason": "test"}

print("\n_secure_once goes straight to the offer URL")
page = DirectPage(body="Place Order Cancel Order")   # a live basket
result = buyer._secure_once(DirectSession(page), FakeEvent(), RealListing())
truthy("it navigated somewhere", page.gotos)
target = page.gotos[-1]
check("to the offer URL, with qty=1",
      target, "https://secure.ticketmaster.ie/18006314BD813D3E/lp62f5thgs?qty=1")
falsy("never with qty=0", "qty=0" in " ".join(page.gotos))
truthy("and it recorded taking the direct path", result.used_direct)
truthy("and reached the basket", result.secured)

print("\nthe direct path is skippable, and then the old one runs")
was = config.DIRECT_OFFER
try:
    config.DIRECT_OFFER = False
    page = DirectPage(body="Place Order Cancel Order")

    class TolerantSession(DirectSession):
        def set_quantity(self, qty, result):
            self.qty = qty

        def await_listings(self, result, budget_s):
            return True

    sess = TolerantSession(page)
    try:
        buyer._secure_once(sess, FakeEvent(), RealListing())
    except AssertionError:
        pass   # it reached get_by_role, which is the old path — that is the point
    falsy("no offer URL was built", any("secure.ticketmaster" in g for g in page.gotos))
    check("and the stepper was used instead", getattr(sess, "qty", None), 1)
finally:
    config.DIRECT_OFFER = was

print("\na listing with no id falls back rather than building a broken URL")
page = DirectPage(body="Place Order Cancel Order")


class NoIdListing:
    listing_id = ""
    offer_ids = ()
    section = "STNDNG"


class TolerantSession2(DirectSession):
    def set_quantity(self, qty, result):
        self.qty = qty

    def await_listings(self, result, budget_s):
        return True


sess = TolerantSession2(page)
try:
    buyer._secure_once(sess, FakeEvent(), NoIdListing())
except AssertionError:
    pass
falsy("no offer URL", any("secure.ticketmaster" in g for g in page.gotos))
check("it used the stepper path", getattr(sess, "qty", None), 1)


print("\nan unrecognised landing page falls back rather than pressing on")
# "The URL loaded" is not "the URL worked". A shortcut that carries on into a
# page it cannot identify is a dead end with no second chance, so anything
# that is not a basket and not the listing's own page goes back to the search.
page = DirectPage(body="some page we have never seen before")


class CountingSession(DirectSession):
    def set_quantity(self, qty, result):
        self.qty = qty

    def await_listings(self, result, budget_s):
        return True


sess = CountingSession(page)
try:
    buyer._secure_once(sess, FakeEvent(), RealListing())
except AssertionError:
    pass   # reaching get_by_role IS the fallback engaging
truthy("it tried the offer URL first",
       any("secure.ticketmaster" in g for g in page.gotos))
truthy("then went back to the event page",
       any("/event/18006314BD813D3E" in g for g in page.gotos[1:]))
check("and used the stepper after all", getattr(sess, "qty", None), 1)

print("\na refusal on the direct link also falls back")
page = DirectPage(body="Sorry, these tickets are unavailable")
sess = CountingSession(page)
try:
    buyer._secure_once(sess, FakeEvent(), RealListing())
except AssertionError:
    pass
truthy("it fell back rather than giving up",
       any("/event/18006314BD813D3E" in g for g in page.gotos[1:]))


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}\n")
    sys.exit(1)
print("All checks passed.\n")
