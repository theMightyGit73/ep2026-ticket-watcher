#!/usr/bin/env python3
"""What the page does with a listing, recorded without asking it anything.

Ten distinct listings have now been refused, every one of them carrying
`"active": true` in Ticketmaster's own error payload — including one clicked
5.3 seconds after the sweep saw it. That rules out speed as the explanation
and leaves two that this project cannot yet tell apart:

  * the listing really is in somebody else's basket, or
  * this client is not allowed to make an offer at all.

They call for opposite responses — wait, or stop waiting and buy by hand — and
the evidence that separates them is the request that produced the q404 and
what it said. The landing page says "not found" and nothing more.

So OfferTrace listens to traffic the attempt was making anyway. The
alternative — deriving an offer URL from `offerIds` and firing it — is
guesswork against an endpoint never observed, on a connection blocked
twenty-three times, and it is exactly how the next block gets earned.

The tests that matter most here are the redaction ones. This browser carries a
live Ticketmaster session, and a trace written to disk must never be a file
that lets its reader become David.

Run with:  .venv/bin/python tests/test_offer_trace.py
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _sandbox  # noqa: F401  (isolates state, profiles and diagnostics)

from ep_watcher import buyer
from ep_watcher.model import Listing
from ep_watcher.sources.browser import _listing_from_pick

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

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}\n")
    sys.exit(1)
print("All checks passed.\n")
