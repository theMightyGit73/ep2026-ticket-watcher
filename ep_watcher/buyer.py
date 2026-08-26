"""Secure a resale listing in a basket. Never pay for it.

The watcher spent its first week being very good at the half of the job that
turned out not to be the hard half. It found six real listings on 2026-08-18
and alerted on every one; David reached none of them in time. His account of
why is specific and matches the data: by the time he has opened the page, set
the quantity and searched, the listing is either gone or refuses on the next
screen because it is sitting in somebody else's basket.

So this module closes that gap, and only that gap. It clicks into a listing
the moment the watcher sees one, puts it in a basket, and stops dead. It does
not enter payment details, does not confirm an order, and has no code path
that could. The hold is then David's to complete on the same machine — a
Ticketmaster basket lives in the session that created it, so the handoff is
"walk to this laptop", not "click a link on your phone".

Two browsers, deliberately
--------------------------
The watcher's own browser (config.PROFILE_DIR) stays signed OUT and does all
the polling. This one (config.BUY_PROFILE_DIR) is signed in and only ever
opens when a real listing exists. On 2026-08-18 that would have been six
openings against 140 polls, which is the ratio that keeps his account away
from the traffic that gets connections blocked.

What is verified and what is not
--------------------------------
The listing-row selectors below are built from the page text captured in the
find recordings of 2026-08-18 — the "Verified Resale Ticket" row, its section
line and its price. They have NOT been driven through to a basket against a
live listing, because no listing has been live since this was written. The
flow is written to fail loudly and harmlessly: every step that cannot find
what it expects records why and returns `secured=False`, and the ordinary
alert still goes out. Treat the first real find as the test.
"""

import base64
import gzip
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Optional

from . import config

# ── Knowing whether the buying profile is signed in ──────────────────────────
#
# This was originally `"sign out" in page_text or "my account" in page_text`,
# copied from the watcher's own login command. On 2026-08-19 that was checked
# against every page capture the watcher has ever taken and found to be
# useless in both directions: not one of the nine recordings contains "sign
# out", "my account" OR "sign in". Ticketmaster does not put the account
# control anywhere that Playwright's flattened `inner_text` can see it, so
# the test would have answered "not signed in" for a perfectly good session —
# and the buyer would have refused to act on the first real listing after
# David had signed in correctly.
#
# Cookies are the honest signal, but presence alone is not enough either: the
# signed-OUT watcher profile already carries 33 ticketmaster.ie cookies, all
# of them analytics and consent. What distinguishes a signed-in profile is
# WHICH names are present, and the only moment anybody can know that for
# certain is the moment a human says "I have just signed in".
#
# So `login-buy` records the names it sees at that moment, and everything
# afterwards compares against that recording. A guess made once, by a human,
# beats a guess hard-coded by someone who has never seen the page.

#: Cookie names present on a signed-OUT ticketmaster.ie profile, read from the
#: watcher's own profile on 2026-08-19. Anything in this set proves nothing.
KNOWN_ANONYMOUS_COOKIES = {
    "mt.v", "_ga", "BID", "_scid", "_scid_r", "cto_bundle", "__gads", "__gpi",
    "LANGUAGE", "_au_1d", "OptanonConsent", "OptanonGroups", "__spdt",
    "eupubconsent-v2", "_gcl_au", "_fbp", "_uetvid", "_uetsid",
}

#: Recorded at sign-in, but worthless as evidence of one.
#:
#: These appear on a signed-in profile and not on the signed-out baseline, so
#: record_signed_in_fingerprint collects them as "account cookies" — and then
#: their absence is read as having been signed out. They are nothing of the
#: kind:
#:
#:   * KP_UIDz / KP_UIDz-ssn are Kasada's bot-detection tokens. They are
#:     reissued constantly and are cleared outright whenever the browser
#:     identity is refreshed, which this watcher does every 90 minutes on
#:     purpose.
#:   * ma.paramsToken and SOTC are short operational cookies the site reissues
#:     on the next page load. SOTC was observed carrying a two-hour expiry.
#:   * ma.LANGUAGE is a language preference.
#:
#: Measured on 2026-08-20, when doctor reported the buying profile signed out
#: while nine of its eleven recorded cookies were present and healthy —
#: including id-token with a month left. The two missing ones were the Kasada
#: pair. `login-auto` then went to sign in and found Ticketmaster serving an
#: ACCOUNT page rather than a sign-in form, which settled it: the session was
#: fine and the check was wrong.
#:
#: This is the second time this exact lesson has been learned. Commit 4938c25
#: stopped the session being judged by the EXPIRY of a cookie designed to
#: churn; it left the PRESENCE test judging by the same cookies. Both halves
#: are needed, and the cost of getting it wrong is not a failed hold — securing
#: attempts anyway — but a warning that cries wolf in every hourly email, which
#: is how a real signed-out warning ends up skimmed past.
CHURNING_COOKIES = frozenset({
    "KP_UIDz", "KP_UIDz-ssn", "ma.paramsToken", "SOTC", "ma.LANGUAGE",
})

#: Prefixes of cookies that are analytics whatever else is true of them.
#:
#: The signed-out baseline catches most of these, but not all: Google
#: Analytics mints a per-property cookie (_ga_MNQMF2C2CB) that only appears
#: once you have visited the pages behind a sign-in, so it looked like part of
#: the account. It carried a 2027 expiry, and reporting the longest-lived
#: "account" cookie therefore announced the session was good for 400 days.
#: That is exactly as wrong as the two hours it replaced.
ANALYTICS_PREFIXES = ("_ga", "_gid", "_gcl", "_fbp", "_uet", "_scid", "__gads",
                      "__gpi", "cto_", "_au_", "_pn_", "permutive", "_ddl")


def _stamp() -> str:
    """Local shim for state.stamp(), imported lazily to avoid a cycle."""
    from .state import stamp

    return stamp()


#: The dead end a refused listing lands on, e.g.
#: https://secure.ticketmaster.ie/error/q404?cid=<uuid>&ctx=<blob>
ERROR_URL_RE = re.compile(r"secure\.ticketmaster\.[a-z.]+/error/", re.I)


def read_error_context(url: str) -> dict:
    """Decode the `ctx` blob Ticketmaster puts in its own error URL.

    The single most valuable thing this project has found, and it was sitting
    in a field the code already captured and never read.

    When a click into a listing is refused, the browser lands on
    `secure.ticketmaster.ie/error/q404?cid=…&ctx=…`. That `ctx` is a
    URL-encoded, base64'd, gzipped JSON document, and it contains
    Ticketmaster's own record of the listing it just refused:

        {"event": {...},
         "listing": {"id": 41966773, "urlId": "lw09yvzt", "active": true,
                     "offerType": "Three+ Presale Ticket", "section": "STNDNG",
                     "sellPrice": 310.5, "isGeneralAdmission": true, ...}}

    `active` is the field that matters, and it changes what this project
    believes about itself. Every one of the fifteen refusals recorded between
    2026-08-21 and 2026-08-24 carries `"active": true` — including the attempt
    of 2026-08-24 10:07, which reached the click 5.3 seconds after the sweep
    saw the listing. A listing that is still active is a listing that has not
    sold. So the dominant verdict in the log — "the race being lost at the
    last step" — is wrong about most of the tickets it was written for, and
    the conclusion it invites (shave seconds off the click) is chasing a
    problem that was never there. Five seconds was not fast enough because
    speed was not what refused us.

    Reading it costs nothing. No request, no origin problem, no race: the
    answer is in the URL bar of a page already open, at a moment when the
    resale endpoint CANNOT be asked because the dead end is served from a
    different host. That is precisely the moment the question matters, and
    until now it was the moment nothing could answer it.

    Never raises. Returns {} for a URL that is not an error page, or one whose
    blob does not decode — a diagnostic that throws on the losing path would
    turn a lost ticket into a crash.
    """
    if not url or "ctx=" not in url:
        return {}
    try:
        raw = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("ctx")
        if not raw:
            return {}
        blob = raw[0]
        # Ticketmaster uses the standard alphabet, but the value travels
        # through URL encoding and has been seen with '-' and '_' surviving;
        # accept both rather than losing the payload to an alphabet.
        blob = blob.replace("-", "+").replace("_", "/")
        # Padding is stripped in the URL. Adding too much is harmless —
        # b64decode stops at the first complete quantum — and guessing the
        # exact amount is a needless way to fail.
        data = base64.b64decode(blob + "===")
        text = gzip.decompress(data).decode("utf-8", "replace")
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


#: Query parameters that may carry something worth stealing. Their values are
#: replaced with a length, never recorded.
#:
#: An allowlist would be safer still and is not possible here: the whole point
#: of the capture is that we do not yet know which parameters the offer flow
#: uses, so we cannot enumerate the harmless ones in advance. This is the
#: other half of that bargain — anything that smells of a credential is
#: redacted by name, and the trace is written to a directory that already
#: holds page captures from the signed-in browser.
SECRET_PARAM_RE = re.compile(
    r"(token|auth|session|sid|password|passwd|secret|signature|sig|key|"
    r"bearer|jwt|cookie|csrf|nonce|otp|card|cvv|cvc)", re.I)

#: Headers never written to disk. The buying browser carries David's live
#: Ticketmaster session; a trace that recorded these would be a file on the
#: laptop that lets anyone who reads it become him.
SECRET_HEADERS = frozenset({
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "x-api-key", "api-key",
})

#: Hosts worth recording. Analytics and ad traffic is most of the volume and
#: none of the answer, and leaving it in makes a trace nobody reads.
TRACE_HOSTS = ("ticketmaster.", "livenation.", "ticketweb.")


def _redact_url(url: str) -> str:
    """A URL safe to write down: same shape, no credentials."""
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            return url
        kept = []
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
            if SECRET_PARAM_RE.search(key):
                kept.append((key, f"<redacted:{len(value)}>"))
            elif len(value) > 200:
                # Long blobs are the ctx-style payloads. Keep the head so the
                # shape is recognisable; the decoder can be pointed at the
                # real URL if one is ever needed.
                kept.append((key, value[:200] + f"<+{len(value) - 200}>"))
            else:
                kept.append((key, value))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(kept), parts.fragment))
    except Exception:
        return "<unparseable url>"


class OfferTrace:
    """Record what the page does when a listing is clicked. Observe only.

    The question this exists to answer is the one everything else in this
    module is now blocked on: **which endpoint refuses us, and what does it
    actually say?**

    What we have is the q404 landing page, which is Ticketmaster's error
    screen and says nothing beyond "not found" plus the listing it was not
    found for. What produced it — a navigation, an XHR, a redirect chain, and
    with what status and body — is invisible, so "the listing is in somebody's
    basket" and "this client is not allowed to make offers" remain
    indistinguishable after ten refusals that were all `active: true`.

    A trace settles it, and settles it without a single extra request: this
    listens to traffic the attempt was making anyway. That distinction is the
    whole design. The tempting alternative — construct an offer URL from the
    `offerIds` we can now derive and fire it — would be faster to write and is
    the wrong thing to do twice over: it is guesswork against an endpoint we
    have never seen, and probing unknown URLs on a connection already blocked
    twenty-three times is how the next block gets earned.

    Nothing sensitive is written. Headers in SECRET_HEADERS are dropped
    entirely, credential-shaped query values are replaced by their length, and
    only Ticketmaster-family hosts are recorded at all.
    """

    #: Beyond this many entries, stop recording. A checkout page can make
    #: hundreds of calls, and a diagnostics file nobody can read is a
    #: diagnostics file nobody reads.
    LIMIT = 60

    def __init__(self):
        self.entries = []
        self._attached = None

    def _interesting(self, url: str) -> bool:
        return any(h in (url or "") for h in TRACE_HOSTS)

    def attach(self, page) -> None:
        """Start listening. Never raises — a broken trace must not cost a hold."""
        try:
            page.on("request", self._on_request)
            page.on("response", self._on_response)
            page.on("framenavigated", self._on_nav)
            self._attached = page
        except Exception:
            self._attached = None

    def detach(self) -> None:
        page, self._attached = self._attached, None
        if page is None:
            return
        for event, handler in (("request", self._on_request),
                               ("response", self._on_response),
                               ("framenavigated", self._on_nav)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass

    def _add(self, entry: dict) -> None:
        if len(self.entries) < self.LIMIT:
            entry["at"] = round(time.monotonic(), 3)
            self.entries.append(entry)

    def _on_request(self, request) -> None:
        try:
            if not self._interesting(request.url):
                return
            # Navigations and form posts are the ones that carry an offer.
            if request.method == "GET" and request.resource_type in (
                    "image", "stylesheet", "font", "media", "script"):
                return
            self._add({"kind": "request", "method": request.method,
                       "resource": request.resource_type,
                       "url": _redact_url(request.url)})
        except Exception:
            pass

    def _on_response(self, response) -> None:
        try:
            if not self._interesting(response.url):
                return
            status = response.status
            # Everything that failed, plus the documents that succeeded. A
            # 200 on an image tells us nothing; a 4xx on anything is the point.
            if status < 400 and response.request.resource_type != "document":
                return
            entry = {"kind": "response", "status": status,
                     "url": _redact_url(response.url)}
            if status >= 400:
                # The body of a failure is where the real error code lives —
                # the q404 PAGE says "not found", but whatever the page called
                # to get there may well say why.
                try:
                    body = response.text()
                    entry["body"] = body[:1500] if isinstance(body, str) else ""
                except Exception:
                    entry["body"] = "<unreadable>"
            self._add(entry)
        except Exception:
            pass

    def _on_nav(self, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return
            self._add({"kind": "navigated", "url": _redact_url(frame.url)})
        except Exception:
            pass

    def summary(self) -> list:
        return list(self.entries)


def decode_offer_id(offer_id: str) -> str:
    """What Ticketmaster's `offerIds` actually contain. "" if it will not decode.

    Every one observed is unpadded base32 of `9|{resaleListingId}`:

        HF6GYMRXOQ2GQMTE      -> 9|l27t4h2d       (2026-08-18)
        HF6GYMDXG44WUYZQMZWA  -> 9|l0w79jc0fl     (2026-08-23)

    Which means the handle is not opaque and not something we have to be given
    — it is a pure function of the listing id the sweep already reads.
    """
    if not offer_id:
        return ""
    try:
        # Base32 wants a length that is a multiple of eight; the feed ships it
        # unpadded. Pad up rather than guessing at the exact count.
        pad = (-len(offer_id)) % 8
        return base64.b32decode(offer_id + "=" * pad).decode("utf-8", "replace")
    except Exception:
        return ""


def offer_id_for(listing_id: str) -> str:
    """The offer handle for a listing id, derived rather than waited for.

    The inverse of decode_offer_id(), and the reason both are here: this is
    available the instant the resale feed answers, roughly twenty seconds
    before the current path arrives at the same fact by loading the page,
    setting a quantity, pressing search and waiting for a panel to draw.

    Deliberately unused by the securing flow as it stands. Knowing the handle
    is not knowing the URL that accepts it, and the way to learn that is to
    watch what the page does with it — see OfferTrace — not to guess at
    endpoints. Probing invented URLs would be both a fabricated answer and,
    on a connection already blocked twenty-three times, a good way to earn the
    next block. When the trace shows the real request, this is the piece that
    makes skipping the search possible.
    """
    if not listing_id:
        return ""
    return base64.b32encode(f"9|{listing_id}".encode()).decode().rstrip("=")


def offer_url(event, listing_id: str, qty: int = None) -> str:
    """The checkout URL for one resale listing. "" if it cannot be built.

        https://secure.ticketmaster.ie/{eventId}/{resaleListingId}?qty={n}

    Not a guess. This is the request Ticketmaster's own page makes when a
    resale row is clicked, observed eighteen times in the traces of
    2026-08-24, and in every one of those the listing segment equals
    `decode_offer_id(offerIds[0])` — so the two halves of the URL are exactly
    the two things the resale feed already tells us.

    ── Why this is a fix and not only a shortcut ────────────────────────────

    All eighteen were `?qty=0`.

    Ticketmaster 302s a zero-quantity offer straight to /error/q404, which is
    the "sold or removed" screen this project has spent a fortnight
    explaining. Every securing attempt ever made has asked for ZERO tickets,
    been refused for it, and recorded the refusal as somebody else having
    taken the listing.

    That single field accounts for the whole picture and retires two theories
    at once. Ten distinct listings refused, every one of them `"active": true`
    in Ticketmaster's own error payload — of course they were; nothing was
    wrong with the tickets. One of them was clicked 5.3 seconds after the
    sweep saw it and refused identically — of course it was; speed cannot
    rescue a malformed request. It was never a lost race and never somebody
    else's basket. We were asking for none.

    The page builds that URL from its own quantity state, and the stepper is
    driven with arrow keys because an overlay eats real clicks there — so the
    resale SEARCH goes out as qty=1 while the offer link is built from state
    that never moved off zero. Rather than fight that, this constructs the URL
    from values we already hold and are certain of.

    Passing `qty` explicitly is the point of the function. WANTED_QUANTITY is
    one and must stay one; it is named here so that the number in the URL is
    something this project states rather than something it inherits from a
    control it cannot reliably read.
    """
    from .sources.browser import _event_id_from_url

    if not listing_id:
        return ""
    event_id = _event_id_from_url(getattr(event, "url", "") or "")
    if not event_id:
        return ""
    wanted = config.WANTED_QUANTITY if qty is None else qty
    # A zero here would rebuild the exact bug this exists to fix.
    if not wanted or wanted < 1:
        return ""
    host = "secure.ticketmaster.ie"
    try:
        netloc = urllib.parse.urlsplit(event.url).netloc
        # Follow the event's own domain, so an .ie event goes to the .ie
        # checkout and a future .co.uk one does not silently cross countries.
        if netloc.endswith("ticketmaster.co.uk"):
            host = "secure.ticketmaster.co.uk"
        elif netloc.endswith("ticketmaster.com"):
            host = "secure.ticketmaster.com"
    except Exception:
        pass
    return f"https://{host}/{event_id}/{listing_id}?qty={int(wanted)}"


def uncached_offer_url(url: str, attempt: int) -> str:
    """The offer URL for a RETRY, made distinct so Chrome cannot replay it.

    Returns `url` unchanged for the first attempt, and for every attempt when
    OFFER_NO_CACHE is off.

    ── Why a nonce, after all, and only on retries ──────────────────────────

    The first version of this fix set `Cache-Control: no-cache` on the page
    with `set_extra_http_headers`, and its docstring said it "affects nothing
    else". That was wrong, and the morning of 2026-08-25 is what it cost.

    Playwright's `set_extra_http_headers` is not per-navigation. It is sticky
    for the life of the page, so it did not put no-cache on the offer request
    — it put no-cache on EVERY request the buying browser made afterwards:
    the parked event page, reloaded uncached each time, and every one of the
    relist polls hammering `/api/quickpicks/…/resale`, an endpoint that is
    rate-limited and answers 403 when pushed. The second attempt of the 10:10
    chase never returned at all. The worker sat inside it for 390 seconds,
    which is longer than the ceiling that was supposed to bound it, and while
    it sat there the next listing was refused with "the browser was busy".

    So the scope has to be the request, not the page. A nonce is the only
    thing that is genuinely per-navigation: it changes the cache key for this
    one URL and touches nothing else, cannot be left switched on, and cannot
    block — there is no browser round trip to hang in.

    Applying it only from attempt two is the other half. The first attempt on
    a new listing has nothing cached to replay, so it needs no help, and it is
    also the attempt most likely to succeed — that one goes out as the exact
    URL Ticketmaster's own page builds, with no parameter of ours in it. The
    unknown-parameter risk that argued against a nonce is therefore only ever
    taken on a request that would otherwise be a cache replay: a request that,
    unmodified, is guaranteed to tell us nothing.
    """
    if not config.OFFER_NO_CACHE or attempt <= 1 or not url:
        return url
    sep = "&" if "?" in url else "?"
    # Time-based rather than random so the value is legible in a trace and two
    # retries can be told apart by eye.
    return f"{url}{sep}_={int(time.time() * 1000)}"


def _note_if_cached(result, seconds: float) -> bool:
    """Say so when a 'refusal' never left the machine.

    A navigation that resolves faster than the network possibly can was served
    from cache, and whatever it appears to prove, it proves nothing about the
    listing. Recording that in the attempt's own notes is the difference
    between a chase that can be trusted and the one of 2026-08-24, where ten
    of fourteen retries were replays and every one was filed as fresh evidence
    that the ticket was still held.

    Returns True when the navigation looks like a replay, so callers and tests
    can act on it rather than only reading about it.
    """
    if seconds >= config.CACHE_REPLAY_SECONDS:
        return False
    if result is not None:
        result.cache_replays = getattr(result, "cache_replays", 0) + 1
        result.note(
            f"this navigation came back in {seconds * 1000:.0f}ms, which is "
            f"too fast to have reached Ticketmaster — it was answered from "
            f"the browser cache, so it says nothing about the listing")
    return True


def active_refusal_reason(out) -> str:
    """What to say when Ticketmaster refuses a listing it calls ACTIVE.

    Module-level rather than buried in secure()'s verdict() so that the exact
    wording can be tested. That is not a stylistic preference: the wording is
    the thing that went wrong. This message is the project's own account of
    why it keeps failing, it is what gets read the next morning, and three
    versions of it in a row named a cause that the following day's evidence
    disproved — a lost race, then a basket, then a quantity of zero. Each was
    written as a finding rather than a guess, and each sent the next day's
    work somewhere useless.

    So the rule this encodes: say what was observed, and stop naming a cause.
    Two things are actually known at this point — Ticketmaster refused us, and
    Ticketmaster said the listing was live. The reason for the refusal is not
    in evidence, and the honest record says so rather than filling the gap
    with the most plausible story available.
    """
    replayed = getattr(out, "cache_replays", 0) or 0
    attempts = getattr(out, "attempts", 0) or 0
    minutes = (getattr(out, "elapsed", 0.0) or 0.0) / 60
    summary = getattr(out, "offer_summary", "") or ""
    return (
        f"Ticketmaster refused this listing while its own error page said the "
        f"listing was still ACTIVE. So it had not sold, and this was not a "
        f"race lost at the click — but why we were refused is NOT established. "
        f"We tried {attempts} time(s) over {minutes:.0f} min and were refused "
        f"every time."
        + (f" {replayed} of those never reached Ticketmaster (browser cache), "
           f"so they prove nothing." if replayed else "")
        + (f" Offer: {summary}." if summary else "")
        + " Buy it by hand from the link in the alert."
    )


def describe_offer(listing: dict) -> str:
    """One line about the listing Ticketmaster refused, for an alert.

    Kept beside the decoder because the vocabulary is Ticketmaster's, not
    ours, and both readers of it should see the same words.
    """
    if not listing:
        return ""
    bits = []
    kind = listing.get("offerType")
    if kind:
        bits.append(str(kind))
    section = listing.get("section")
    row = listing.get("row")
    if section:
        bits.append(f"section {section}" + (f" row {row}" if row else ""))
    price = listing.get("sellPrice")
    if isinstance(price, (int, float)):
        fee = listing.get("buyerFeeValue")
        total = price + fee if isinstance(fee, (int, float)) else None
        bits.append(f"€{price:.2f}" + (f" (€{total:.2f} with fees)" if total else ""))
    return " · ".join(bits)


def _is_analytics(name: str) -> bool:
    return any(name.startswith(p) for p in ANALYTICS_PREFIXES)


#: Where the fingerprint taken at sign-in time is kept. Beside the profile
#: rather than inside it, so a Chrome profile reset cannot silently take the
#: evidence with it.
SESSION_FILE = config.BUY_PROFILE_DIR.parent / "buy-session.json"


def _chrome_time(microseconds: int) -> Optional[datetime]:
    """Chrome stores times as microseconds since 1601-01-01 UTC."""
    if not microseconds:
        return None
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=microseconds
        )
    except (OverflowError, ValueError):
        return None


def profile_cookies(profile_dir=None) -> dict:
    """{cookie_name: expiry_or_None} for ticketmaster.ie, read offline.

    Copies the database before reading it. Chrome holds a lock on the live
    file, and this has to work while a browser is open — the alternative is a
    check that only works when the thing being checked is shut, which is no
    check at all.
    """
    profile_dir = profile_dir or config.BUY_PROFILE_DIR
    db = profile_dir / "Default" / "Cookies"
    if not db.exists():
        return {}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copy(str(db), tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, expires_utc FROM cookies WHERE host_key LIKE ?",
            ("%ticketmaster%",),
        ).fetchall()
        conn.close()
        return {name: _chrome_time(exp) for name, exp in rows}
    except (sqlite3.Error, OSError):
        return {}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def anonymous_baseline() -> set:
    """Cookie names a signed-OUT profile on this machine actually carries.

    The watcher's own profile is the baseline, and it is a good one: it is
    real, it is current, and it is guaranteed signed out — staying signed out
    is the whole reason it exists. Anything present in it proves nothing about
    an account.

    Measured rather than assumed, because assuming was wrong. The hardcoded
    KNOWN_ANONYMOUS_COOKIES list was written from a single partial sample and
    missed fifteen ordinary names, so a profile that had never signed in was
    confidently reported as signed in.

    Returns an empty set if the watcher's profile cannot be read, in which
    case the hardcoded list is all there is — weaker, but never worse than
    before.
    """
    try:
        return set(profile_cookies(config.PROFILE_DIR))
    except Exception:
        return set()


def record_signed_in_fingerprint(profile_dir=None) -> dict:
    """Remember what this profile looked like at the moment of signing in.

    Called by `login-buy` once David confirms he is signed in. The cookies
    that are present now but were not on a signed-out profile are, by
    construction, the ones the account is carried in. Nobody has to guess
    their names.
    """
    cookies = profile_cookies(profile_dir)
    # Both baselines: the hardcoded list and the live signed-out profile. The
    # second is what stops fifteen ordinary anonymous cookies being recorded
    # as the account's, which would make every later check meaningless.
    # CHURNING_COOKIES are excluded here as well as at comparison time. Older
    # records already contain them — the one written on 2026-08-19 has five —
    # so the comparison has to filter regardless, but there is no reason to
    # keep writing them into new ones. See CHURNING_COOKIES for what they are
    # and why their absence means nothing.
    auth = sorted(n for n in (set(cookies) - KNOWN_ANONYMOUS_COOKIES
                              - anonymous_baseline() - CHURNING_COOKIES)
                  if not _is_analytics(n))

    # Split by whether they survive the browser closing.
    #
    # Two of the fourteen recorded on 2026-08-19 — TMAUO and ma.SID — carry no
    # expiry at all. Those are session cookies: Chrome drops them when it
    # exits, which it does after every securing attempt. Requiring all
    # fourteen therefore reported a perfectly good profile as signed out the
    # moment the browser had been used once. Only the persistent ones can
    # answer "is this profile still signed in" between runs.
    persistent = sorted(n for n in auth if cookies.get(n))
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "auth_cookies": auth,
        #: The subset the later checks actually compare against.
        "persistent_cookies": persistent,
        "cookie_count": len(cookies),
    }
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(record, indent=2))
    except OSError:
        pass
    return record


def session_evidence(profile_dir=None) -> dict:
    """What can be said about the buying session without opening a browser.

    Returns {signed_in, reason, expires_at, days_left}. `signed_in` is None —
    not False — when there is genuinely no way to tell, because "we cannot
    say" and "definitely signed out" call for different words and different
    actions.
    """
    profile_dir = profile_dir or config.BUY_PROFILE_DIR
    out = {"signed_in": None, "reason": "", "expires_at": None, "days_left": None}

    if not profile_dir.exists():
        out.update(signed_in=False, reason="no buying profile — login-buy has never run")
        return out

    cookies = profile_cookies(profile_dir)
    if not cookies:
        out.update(signed_in=False, reason="the profile holds no ticketmaster cookies")
        return out

    try:
        recorded = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        # No fingerprint: a profile that predates it, or one whose record was
        # lost. Compare against the WATCHER's profile instead of a hardcoded
        # list, because that profile is on this machine, is guaranteed signed
        # out, and is always current.
        #
        # The hardcoded list alone is not good enough and was caught being
        # wrong on 2026-08-19. It was written by hand from one partial sample,
        # so fifteen perfectly ordinary anonymous cookies — SID, TMUO,
        # eps_sid, tmp_id and the rest — were missing from it, and a profile
        # that had never signed in was reported as signed in. That is the
        # dangerous direction: doctor goes green, the banner warning
        # disappears, and the first anyone knows is a listing not being held.
        extra = sorted(set(cookies) - KNOWN_ANONYMOUS_COOKIES - anonymous_baseline())
        if extra:
            out.update(
                signed_in=True,
                reason=f"{len(extra)} cookie(s) that a signed-out profile on "
                       f"this machine does not have (no sign-in fingerprint "
                       f"recorded — re-run login-buy to make this exact)",
            )
        else:
            out.update(signed_in=False,
                       reason="every cookie here is one a signed-out profile "
                              "also has — not signed in")
        return out

    # Only the cookies that survive the browser closing. Session-scoped ones
    # are dropped by Chrome on exit — see record_signed_in_fingerprint — so
    # their absence says nothing about being signed out. Older records that
    # predate the split fall back to filtering the full list the same way.
    expected = set(recorded.get("persistent_cookies") or [])
    if not expected:
        expected = {n for n in (recorded.get("auth_cookies") or []) if cookies.get(n)}
    if not expected:
        out.update(reason="the recorded sign-in found no lasting account cookies "
                          "to watch — re-run the sign-in")
        return out

    # Judge on the cookies that MEAN something, not on the ones that churn.
    #
    # See CHURNING_COOKIES. The recorded fingerprint contains bot-detection
    # tokens and preferences alongside the account, because at sign-in they
    # were simply "present here and not on a signed-out profile". Requiring
    # all of them to survive means a browser-identity refresh — which this
    # watcher performs every 90 minutes by design — reports the account as
    # signed out.
    stable = expected - CHURNING_COOKIES
    if not stable:
        # Everything recorded was a churner. Nothing here can answer the
        # question, and None is the honest verdict: "cannot tell" and
        # "signed out" call for different words and different actions.
        out.update(
            reason="the sign-in fingerprint holds only short-lived cookies, "
                   "which cannot say whether the account is still signed in — "
                   "re-run the sign-in to record a better one",
        )
        return out

    missing = sorted(stable - set(cookies))
    if missing:
        out.update(
            signed_in=False,
            reason=f"the account cookie(s) recorded at sign-in are gone "
                   f"({', '.join(missing[:3])}) — it has been signed out",
        )
        return out

    # When the first account cookie lapses.
    #
    # Not "when the session expires", because nothing here can know that —
    # only Ticketmaster does, and it can invalidate a session server-side at
    # any time regardless of what the cookies say. Two attempts at a
    # confident number both misled on 2026-08-19: the soonest expiry picked
    # SOTC and announced "0.1 days" on a healthy profile, and the longest
    # picked a Google Analytics cookie and announced 400 days.
    #
    # So this reports the earliest lapse among real account cookies, which is
    # the first moment anything is known to change, and the callers word it as
    # that rather than as a guarantee.
    # PRESENCE is the verdict. Expiry is information, and only information.
    #
    # This used to flip signed_in to False the moment the earliest-expiring
    # recorded cookie passed its date, and at 21:00 on 2026-08-19 that
    # declared a perfectly good session dead. The eleven cookies recorded at
    # sign-in are not one kind of thing: `id-token` is the account and had 29
    # days left, while SOTC, KP_UIDz-ssn and ma.paramsToken are short-lived
    # operational cookies the site reissues on the next page load. Judging the
    # session by the soonest of those is judging it by the part designed to
    # churn.
    #
    # It failed in the dangerous direction. doctor, check-buy and the startup
    # banner all went red, and the fix they printed was to sign in again —
    # which for an account already signed in means putting a password through
    # a scripted login for no reason, against the very account the
    # two-browser design exists to keep away from attention.
    #
    # A cookie still IN the profile has not been dropped by Chrome. If the
    # account cookies genuinely go, the `missing` check above catches it, and
    # that is the check that carries meaning.
    now = datetime.now(timezone.utc)
    churned = sorted((expected & CHURNING_COOKIES) - set(cookies))
    if churned:
        # Worth saying, and worth saying as normal. Somebody comparing this
        # output against the recorded fingerprint by hand will notice the
        # difference and should not have to wonder whether it matters.
        out.update(
            signed_in=True,
            reason=f"the account cookies are present; "
                   f"{', '.join(churned[:3])} rotated away, which is what "
                   f"those do and says nothing about the account",
        )
    else:
        out.update(signed_in=True,
                   reason="the account cookies recorded at sign-in are all present")

    # The next real change is the soonest expiry still in the FUTURE. A date
    # already passed on a cookie that is nonetheless present describes one
    # mid-reissue, not a session ending.
    future = [cookies[n] for n in stable if cookies.get(n) and cookies[n] > now]
    soonest = min(future) if future else None
    if soonest:
        # Four decimals, not one: rounding days to 1dp collapsed everything
        # under about 72 minutes to 0.0, and describe_lapse read 0.0 as
        # "already" — reporting a cookie 54 minutes from lapsing as gone.
        left = (soonest - now).total_seconds() / 86400.0
        out.update(expires_at=soonest.isoformat(), days_left=round(left, 4))
    else:
        out.update(reason="the account cookies recorded at sign-in are all "
                          "present, though every recorded expiry has passed — "
                          "they are being reissued, which is normal")
    return out


def profile_in_use(profile_dir=None) -> bool:
    """Is a Chrome already running on this profile directory?

    Chrome takes an exclusive lock on a user-data-dir, and the buying browser
    is deliberately LEFT OPEN after a successful hold, because closing it is
    what drops the basket. Those two facts collide on the second find of a
    busy afternoon: the first hold's window is still up, the profile is
    locked, and Playwright fails with a message about a singleton lock that
    says nothing about a ticket.

    Six real listings appeared on 2026-08-18, and eight sightings fell inside
    one day, so two finds inside a fifteen-minute hold window is an ordinary
    Tuesday rather than a corner case.

    Asked of the process table rather than of Chrome's own SingletonLock file
    in the profile, which survives a crash and would report the profile busy
    forever afterwards. Any error answers False: this gate must never be the
    reason a real listing goes unheld, so when it cannot tell, the attempt
    goes ahead and fails honestly on its own terms.
    """
    from pathlib import Path

    profile_dir = Path(profile_dir or config.BUY_PROFILE_DIR)

    # Anchored on the end of the argument, and that anchor is the whole
    # correctness of this function. `pgrep -f` matches a substring of the
    # command line, so an unanchored "user-data-dir=.../chrome-profile" also
    # matches ".../chrome-profile-buy" — asking whether the WATCHER's profile
    # was busy would have answered yes whenever the buying browser was open,
    # and the poll loop would have reset a perfectly good profile. It is the
    # same mistake restart.sh made in the other direction, found the same way,
    # and caught here only because the test for this function asked about two
    # profiles sharing a prefix.
    #
    # The path is escaped because it is a regular expression to pgrep, and a
    # real one contains a dot: ".ep2026-watcher" would otherwise match
    # "Xep2026-watcher" too.
    specials = ".[]()*+?{}|^$\\"
    quoted = "".join("\\" + ch if ch in specials else ch for ch in str(profile_dir))
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={quoted}( |$)"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return found.returncode == 0 and bool(found.stdout.strip())


def release_buying_browser(profile_dir=None) -> bool:
    """Close the buying browser, dropping whatever it is holding. True if one died.

    Only ever called to make room for a HIGHER priority ticket — see
    Event.secure_priority. It throws away a live basket, which is normally the
    worst thing this codebase can do, and is right in exactly one case: an
    Early Entry pass is being held and a Weekend Ticket has appeared. The pass
    is only valid alongside a weekend ticket, so holding it while the ticket
    goes by spends the one buying browser on the one product that is useless
    on its own.

    Done by killing the process rather than by calling close() on the session,
    and that is not laziness. The session was created inside the securing
    thread, and Playwright's sync objects belong to the thread that made them;
    closing one from another thread fails in the same family of ways that made
    the threading necessary in the first place. The process table does not
    care which thread is asking.

    The pattern is anchored on the end of the argument for the same reason
    restart.sh's is: `pkill -f` matches substrings, and an unanchored buy
    profile path would also match a longer one.
    """
    from pathlib import Path

    profile_dir = Path(profile_dir or config.BUY_PROFILE_DIR)
    specials = ".[]()*+?{}|^$\\"
    quoted = "".join("\\" + ch if ch in specials else ch for ch in str(profile_dir))
    try:
        killed = subprocess.run(
            ["pkill", "-f", f"user-data-dir={quoted}( |$)"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # Chrome needs a moment to let go of the profile lock, or the browser we
    # are about to open for the weekend ticket fails on the way in.
    if killed.returncode == 0:
        time.sleep(2.0)
        return True
    return False


@dataclass
class HoldResult:
    """What came of trying to secure one listing."""

    #: True only when a basket was positively confirmed on the page. Never
    #: inferred from the absence of an error — a hold nobody can see is worse
    #: than no hold, because it sends David to a screen with nothing on it.
    secured: bool = False
    #: Why not, in words fit for an alert. Empty when secured.
    reason: str = ""
    notes: List[str] = field(default_factory=list)
    #: How long he has, for wording only. Read off the checkout page's own
    #: countdown when one is visible, otherwise config.HOLD_MINUTES_HINT.
    minutes_hint: int = 0
    #: Where the checkout is, captured the moment a basket is confirmed.
    #: Offered to David's phone as worth trying — see the note at the capture
    #: site. Empty when nothing was held.
    checkout_url: str = ""
    #: True when an earlier, lower-priority hold was dropped to attempt this
    #: one. The alerts say so either way: he needs to know the Early Entry
    #: pass was let go, whether or not the weekend ticket was then caught.
    preempted: bool = False
    #: True when minutes_hint was read from the page rather than estimated.
    #: The alert says which, because "you have about ten minutes" and "the
    #: page says 11:39" deserve different amounts of trust — and the estimate
    #: comes from one observation of an entirely different event.
    minutes_measured: bool = False

    #: True once 'Continue To Payment' has been pressed — the step
    #: Ticketmaster says reserves the ticket. Distinct from `secured`, which
    #: only means a checkout page was reached: on the capture of 2026-08-26
    #: the page said in its own words that the tickets were NOT reserved
    #: until that button was pressed, so the alert has to be able to tell
    #: David which of the two he is looking at.
    reserved: bool = False

    #: How many navigations in this attempt were answered by Chrome rather
    #: than by Ticketmaster — see _note_if_cached().
    #:
    #: Recorded because a chase's honesty depends on it. Any refusal counted
    #: here observed nothing, so a chase whose replays outnumber its real
    #: requests has not established that a listing is still held; it has
    #: established that nobody asked. On 2026-08-24 that was ten attempts in
    #: fourteen, and the whole basket theory rested on them.
    cache_replays: int = 0

    #: Seconds spent on each step, in the order they happened.
    #:
    #: Added 2026-08-20, because the race was being tuned on inference. Two
    #: weekend listings at €366.39 were found and lost that day, and the best
    #: anyone could say about why was "roughly sixty seconds, probably" —
    #: derived from minute-resolution log lines. That is not a measurement,
    #: and you cannot optimise against it.
    #:
    #: Now every failed hold says exactly where its seconds went, which turns
    #: the next lost ticket from a shrug into a number. It also settles
    #: whether keeping the buying browser warm was worth it, rather than
    #: leaving that as an opinion.
    timings: "OrderedDict[str, float]" = field(default_factory=OrderedDict)
    #: When the attempt began, for total elapsed.
    started_at: float = field(default_factory=time.monotonic)
    #: The clock the next mark() measures from.
    _last_mark: float = field(default_factory=time.monotonic)
    #: When the last attempt stopped, so `elapsed` freezes instead of counting
    #: the time spent emailing about the failure afterwards. None while an
    #: attempt is still running.
    finished_at: Optional[float] = None

    # ── Forensics for a lost race ────────────────────────────────────────────
    #
    # Added 2026-08-20 after two weekend listings were found and lost within
    # half an hour, both with a complete and fast pipeline: the row was located
    # in 0.0s and the click landed on "sold or removed from sale". The timings
    # said the attempt took 14 and 17 seconds, which sounds like a speed
    # problem — but nothing in the record could distinguish the two
    # explanations, and they call for opposite responses:
    #
    #   * SOLD. Somebody genuinely bought it in those seconds. The answer is to
    #     be faster, and every second is worth chasing.
    #   * HELD, or never purchasable. The listing is in another buyer's basket,
    #     or the feed is advertising something the offer flow will not honour.
    #     Then being faster wins nothing at all, because there is nothing to
    #     win — and the answer is to WAIT and re-attempt, since baskets expire.
    #
    # The endpoint can tell them apart, and it is one call. If the listing is
    # still in the feed immediately after Ticketmaster has said it is gone,
    # then "sold" is not what happened.
    #
    #: Was the listing still in the resale feed at the moment of failure?
    #: True/False, or None when the endpoint could not be asked.
    still_listed_after: Optional[bool] = None
    #: The listing ids the feed returned at that moment, for comparison with
    #: the id the find was reported under. These ids have been observed to
    #: change between polls for what is plainly the same listing, so an id that
    #: differs is evidence about the feed rather than about the ticket.
    ids_after: List[str] = field(default_factory=list)
    #: The id this attempt set out to secure.
    listing_id: str = ""
    #: Where the click actually landed. Captured on failure as well as on
    #: success, because the URL of the dead end is the only place the direct
    #: link to a listing has ever been visible — and a direct link is what
    #: would let a future attempt skip the search entirely.
    landed_url: str = ""
    #: Seconds between the listing being SEEN and this attempt starting. The
    #: step timings only measure the attempt; the sweep that found it may have
    #: been up to its whole interval behind, and that latency is invisible in a
    #: report that starts its clock when the buyer wakes up.
    detected_age: Optional[float] = None
    #: How many goes it took. More than one only ever happens when the feed
    #: said the ticket had not really sold — see secure().
    attempts: int = 1
    #: Did ANY attempt see the listing still in the feed after being refused?
    #:
    #: Separate from still_listed_after, which is cleared between attempts so
    #: each probe answers for itself. This remembers across them, because the
    #: sequence tells a different story than its last line does.
    #:
    #: Observed twice on 2026-08-21, at 12:20 and 12:25, in the same shape: the
    #: first attempt is refused while the feed still lists the very id we
    #: tried, and the second — twenty seconds later — finds it genuinely gone.
    #: Reporting only the final answer calls that "the race being lost at the
    #: last step", which is precisely wrong. We never had a race. The ticket
    #: was already in somebody's basket when we first reached it, and that
    #: somebody then paid. Those are different problems with different fixes:
    #: one says be faster at the click, the other says the click was never
    #: going to work and the lever is seeing the listing sooner.
    ever_listed_after: bool = False
    # ── What Ticketmaster's own error page says ──────────────────────────────
    #
    # Decoded from the `ctx` blob on the dead end, at no cost — see
    # read_error_context(). This is a statement by Ticketmaster about the
    # listing it has just refused, made at the moment of refusal, and it
    # outranks every inference this module makes from the feed:
    #
    #   * The feed answers "is this listing OFFERABLE to me right now", and a
    #     ticket in somebody else's basket drops out of it. So a listing
    #     vanishing from the feed has always been read here as "it sold", and
    #     for a basket that is exactly wrong — the ticket is still for sale,
    #     it is merely spoken for, and it comes back when the basket lapses.
    #   * `active` answers "does this listing still exist", which is the
    #     question the retry actually turns on.
    #
    #: True when the error page says the listing is still active — i.e. it did
    #: NOT sell, whatever the feed says. None when there was no error context
    #: to read (no click, or a failure before one).
    listing_active: Optional[bool] = None
    #: Ticketmaster's own name for what kind of ticket this is, e.g.
    #: "Three+ Presale Ticket" or "General Admission Tier 2 - 3rd and Final
    #: Payment .BO". Recorded because the refusals are not evenly spread
    #: across it, and a type that is never honoured is worth knowing about
    #: before spending the buying browser on it again.
    offer_type: str = ""
    #: The listing line as Ticketmaster describes it — type, section, price.
    offer_summary: str = ""
    #: Did this attempt go straight to the offer URL rather than clicking the
    #: row? Recorded so the first hold can be attributed, and so "the direct
    #: path works" stops being a claim and becomes a column.
    used_direct: bool = False
    #: Where the clicked row pointed, if it pointed anywhere. An href here is
    #: the direct path; an empty string after a click means the row was
    #: scripted and there is no URL to shortcut to.
    row_href: str = ""
    #: What the page did between the click and the dead end — see OfferTrace.
    #: Observation only, and the reason it exists: the q404 screen says "not
    #: found" and nothing else, so the endpoint that refused us and its real
    #: error body have never once been seen.
    trace: List[dict] = field(default_factory=list)
    #: Ticketmaster's own offer handles for the listing we went after, from
    #: the resale feed. Base32-decoded these read `9|{resaleListingId}`.
    offer_ids: tuple = ()
    #: Did ANY attempt see Ticketmaster call this listing active?
    #:
    #: The counterpart of ever_listed_after, and it exists for the same reason:
    #: listing_active is cleared between attempts so each refusal answers for
    #: itself, but the sequence tells a story its last line does not. A ticket
    #: that was active when we first reached it and inactive twenty minutes
    #: later was never a race we lost by being slow — it was one somebody else
    #: was allowed to complete while we were told no.
    ever_active: bool = False
    #: Ticketmaster served a block or challenge screen instead of the page.
    #:
    #: Its own field rather than a phrase in `reason`, because it decides
    #: whether going back is worth anything. A challenge is transient — it is
    #: the client being asked to wait, not the ticket being gone — so it is
    #: the second condition worth retrying, alongside a listing the feed still
    #: shows. Without this, three attempts on 2026-08-22 each stopped after a
    #: single try: the block fails before the resale panel, so
    #: still_listed_after was never set, and secure() read that as "nothing to
    #: come back for".
    challenged: bool = False

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"    [buyer] {text}")

    def mark(self, step: str) -> float:
        """Record the seconds spent since the previous mark. Returns them.

        Deliberately measures the GAP rather than the total, so the steps sum
        to the elapsed time and the slow one is obvious at a glance. Repeated
        step names accumulate rather than overwrite — a retry is still time
        spent on that step, and hiding it would flatter exactly the step that
        needs looking at.
        """
        now = time.monotonic()
        spent = now - self._last_mark
        self._last_mark = now
        self.timings[step] = self.timings.get(step, 0.0) + spent
        return spent

    @property
    def elapsed(self) -> float:
        """Wall-clock seconds the whole attempt took, retries included.

        The honest total, and deliberately not the sum of `timings`. A step
        that FAILS is never marked — mark() runs after the thing it measures —
        so the step sum omits precisely the step worth timing. The attempt of
        2026-08-21 05:57 summed to 10.01s and really ran about twenty-five:
        ten seconds setting the quantity, then a fifteen-second timeout on a
        search button that never appeared. Every attempt this project has ever
        logged is a failed one, so every total it has ever printed was short.

        Frozen at finished_at once the attempt is over, so the number does not
        keep growing while the failure email is being written.
        """
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def timing_line(self) -> str:
        """One line for the log and the email, slowest step called out.

        Empty when nothing was measured, so callers can drop it rather than
        print a heading over nothing.
        """
        if not self.timings:
            return ""
        parts = " ".join(f"{k} {v:.1f}s" for k, v in self.timings.items())
        measured = sum(self.timings.values())
        slowest = max(self.timings.items(), key=lambda kv: kv[1])
        line = (f"{parts} | total {self.elapsed:.1f}s "
                f"| slowest: {slowest[0]} at {slowest[1]:.1f}s")
        # Say so when the steps do not account for the time. The gap IS the
        # diagnosis: it is a step that timed out rather than completed, and
        # reporting only the measured seconds would hide the slowest thing
        # that happened behind the fastest number available.
        unmeasured = self.elapsed - measured
        if unmeasured > 1.0:
            line += (f" | {unmeasured:.1f}s unaccounted for — a step that "
                     f"timed out rather than finished")
        return line


class BuySession:
    """A signed-in Chrome, opened on a find and held while a basket is live.

    Deliberately not a long-lived singleton like the watcher's session. It
    exists for the length of one attempt plus however long David needs to pay,
    and closing it is what releases the hold — so it is closed by the caller,
    explicitly, never by a timeout in here.
    """

    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir or config.BUY_PROFILE_DIR
        self._session = None

    def start(self):
        # Imported here, not at module scope, so that importing this module —
        # which the tests and the alerting path both do — never costs a
        # Playwright import or requires it to be installed.
        from .sources.browser import BrowserSession

        # Headed and ON SCREEN, both load-bearing. Headless gets 403 from
        # Ticketmaster, and offscreen would park the window at -2400 where he
        # cannot finish paying in it — which is the entire point of the
        # session existing.
        was_offscreen = config.OFFSCREEN
        config.OFFSCREEN = False
        try:
            self._session = BrowserSession(headless=False, profile_dir=self.profile_dir)
            self._session.start()
        finally:
            config.OFFSCREEN = was_offscreen
        return self

    def close(self):
        """Closing releases the basket. Only the caller decides when."""
        if self._session is not None:
            self._session.close()
            self._session = None

    @property
    def page(self):
        return self._session.page

    def await_listings(self, result: "HoldResult", budget_s: float) -> bool:
        """Wait until the resale panel can actually be read. True if it can.

        The reason the first three real securing attempts all failed.
        
        Pressing search does not produce listings. The search resolves, and
        only THEN does a separate call to /api/quickpicks/{event}/resale come
        back and render "Other Options → Verified Resale Tickets" — a fact the
        watcher's own module establishes at length, because reading the page
        too early is what once recorded a quarter of its polls as resale-blind.

        The buyer ignored all of that. It clicked search and looked for the
        listing row five seconds later, which on the watcher's own measurements
        is well before the panel exists. So it reported "the listing was gone
        from the page by the time the buying browser reached it" on 2026-08-19
        at 17:58, 19:05 and 19:12 — three real listings, each almost certainly
        still sitting there, each recorded as sold.

        That verdict was worse than the failure. It read as losing a race,
        which invites making the watcher faster; the actual fault was looking
        before the page had drawn anything, which no amount of speed fixes.

        Reuses the watcher's own two waits rather than reimplementing them.
        They encode a fortnight of findings about when this page is readable,
        and a second copy would drift from the first.
        """
        session = self._session
        deadline = time.monotonic() + budget_s

        # Two thirds of what is left for the search to come back, because a
        # search that has not resolved cannot have a panel under it.
        outcome = session._await_result(timeout_s=max(5.0, (deadline - time.monotonic()) * 0.66))
        if outcome == "basket":
            result.note("the search went straight to a basket")
            return True
        if outcome == "timeout":
            result.note("the search did not resolve in time — the page is slow, "
                        "not necessarily empty")
            return False
        result.note("search resolved")

        left = deadline - time.monotonic()
        if left <= 0:
            return False
        readable, why = session._await_resale_panel(
            timeout_s=max(5.0, left),
            render_s=min(8.0, max(2.0, left / 3.0)),
        )
        result.note(f"resale panel: {'readable' if readable else why}")
        return readable

    def listings_now(self, event, qty: int):
        """Ask the resale endpoint what is actually on offer, right now.

        Used to tell "the listing has sold" apart from "the page has not drawn
        it yet" — which the row hunt alone cannot do, and which decides whether
        a failure means the race was lost or the code looked too early.
        """
        return self._session.fetch_resale_json(event, qty)

    def listings_from_origin(self, event, qty: int):
        """The same question, asked from a page that can actually ask it.

        `listings_now` fetches a relative URL from whatever page the browser is
        standing on. That is deliberate and right on the event page — it is the
        call the page makes for itself, carrying its cookies, its origin and
        its TLS fingerprint, which is the whole reason it works where a Python
        HTTP client gets a 403.

        It is useless at the moment it is needed most. The dead-end screen a
        refused listing lands on is served from `secure.ticketmaster.ie`, while
        the endpoint is same-origin to `www.ticketmaster.ie`, so the relative
        fetch resolves against the wrong host and cannot answer at all. EVERY
        attempt that reaches that screen is in that position — which means the
        forensic built to decide "did it sell, or was it never takeable" has
        never once been able to run on the path it was built for. Four of the
        fourteen recorded losses assert the feed agreed the ticket was gone,
        and not one of them asked it.

        That question is not a detail. It decides the whole strategy: if these
        listings are real and we are simply slow, then every second is worth
        chasing; if they are advertised but not purchasable, then chasing
        seconds wins nothing at all and the answer is somewhere else entirely.
        Two listings on 2026-08-21, at 11:19 and 11:33, were lost in 6.6s and
        7.9s — fast enough that "we were slow" is starting to look like the
        weaker explanation.

        So this opens a second tab in the SAME browser context, which carries
        the same cookies and the same signed-in session, parks it on the event
        page and asks from there. It costs a page load, spent only on a path
        where the ticket is already lost and nothing is racing any more.
        """
        page = None
        try:
            ctx = getattr(self._session, "_ctx", None)
            if ctx is None:
                return None
            page = ctx.new_page()
            page.goto(event.url, wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            # Borrow the session's own fetch, pointed at this page. Same code
            # path as every other resale read — a second implementation here
            # is exactly the duplication that let the two readers of this
            # endpoint disagree in the first place.
            was, self._session._page = self._session._page, page
            try:
                return self._session.fetch_resale_json(event, qty)
            finally:
                self._session._page = was
        except Exception:
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def set_quantity(self, qty: int, result: "HoldResult") -> None:
        """Drive the page's quantity stepper, reusing the watcher's logic.

        The stepper is a role=spinbutton driven with arrow keys because the
        page floats an overlay over it that eats real clicks — knowledge that
        cost a day to find and must not be duplicated here and left to drift.
        """
        self._session._set_quantity(qty, _NoteSink(result))

    # There is deliberately no `signed_in()` method here.
    #
    # There was one, and it asked `"sign out" in page_text or "my account" in
    # page_text` — the exact test the module header above shows was checked
    # against every page capture the watcher has ever taken and found in none
    # of them. Ticketmaster renders the account control somewhere Playwright's
    # flattened inner_text cannot reach, so it answered "signed out" for a
    # perfectly good session.
    #
    # By the time that was established the method had no callers, which made
    # it more dangerous rather than less: dead code that looks like the
    # obvious answer is what the next person reaches for. Ask
    # session_evidence() instead — it reads the cookie database, and it is
    # what secure(), doctor and check-buy all already use.


class _ParkNotes:
    """Swallow the note() calls set_quantity makes while nobody is listening.

    _NoteSink writes into a HoldResult, and there is no attempt in progress
    when the browser is merely parking. Printing them would put "quantity set
    to 1" into the log after every find — noise that reads like something
    happening.
    """

    def note(self, text: str) -> None:
        pass


class BuyerWorker(threading.Thread):
    """A signed-in browser kept open and warm, waiting for a listing.

    Measured on 2026-08-20: two weekend listings at €366.39 were found and
    lost, and once the detection lag was fixed the entire remaining gap was
    this — about sixty seconds between seeing a listing and clicking its row,
    spent almost wholly on work that could have been done in advance. A fresh
    Chrome was launched, the event page loaded through its 401-then-reload
    dance, and only then did the part that depends on the listing begin. These
    listings are consumed in well under a minute.

    So the browser is opened once, at startup, and parked on the event page.
    When a listing appears the attempt begins at the search.

    THE THREAD IS THE POINT, not an optimisation. Playwright's sync objects
    belong to the thread that created them, which is why securing already ran
    in a thread of its own — see secure_in_thread. A browser created at
    startup and then driven from a different thread fails in exactly the
    family of ways that made the threading necessary in the first place.

    That constraint shapes the whole class: ONE thread owns the session for
    its entire life, and every operation that touches the page — attempting,
    re-parking, dropping a basket to make room, shutting down — is a job on a
    queue rather than a method that reaches in from outside. The public
    methods only ever put things on the queue and read a state string.

    Everything degrades. If the worker cannot start, has died, or is busy,
    secure_in_thread falls back to the cold-start path that has always been
    there. A warm browser is a speed-up, never a dependency.
    """

    #: Re-load the parked page this often, so a browser left open for hours is
    #: not asked to act on a page rendered before lunch. One page load per
    #: interval, spent while nothing is happening rather than during a race.
    REFRESH_MINUTES = float(os.environ.get("EP_BUY_WARM_REFRESH", "20"))

    #: How long to wait at startup for the browser to come up. Generous: it
    #: happens once, alongside the watcher's own browser starting.
    STARTUP_TIMEOUT = float(os.environ.get("EP_BUY_WARM_STARTUP", "120"))

    def __init__(self, home=None):
        super().__init__(name="ep-buyer-warm", daemon=True)
        #: The page to sit on while idle — the most important securable one,
        #: so the commonest find needs no navigation at all.
        self.home = home or next(
            (e for e in config.EVENTS if e.secure), config.EVENTS[0])
        self._jobs = queue.Queue()
        self._session = None
        self._parked_on = None
        self._lock = threading.Lock()
        #: "starting" -> "idle" <-> "busy" -> "holding" -> "idle" | "dead"
        self._state = "starting"
        self._ready = threading.Event()
        self._stop = threading.Event()

    # ── state, safe to read from any thread ──────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set(self, state: str) -> None:
        with self._lock:
            self._state = state

    @property
    def available(self) -> bool:
        """Idle and alive, so it can take a job now."""
        return self.state == "idle" and self.is_alive()

    @property
    def holding(self) -> bool:
        """Is a basket live in this browser?

        This is what replaces profile_in_use() once a browser is kept warm.
        That check greps the process table for a Chrome on the buying profile,
        which was a fair proxy while the browser existed only during an
        attempt — and becomes permanently true the moment one is warm, which
        would refuse every job for ever. Asking the worker is both cheaper and
        actually correct.
        """
        return self.state == "holding"

    # ── the thread: the only place the page is ever touched ──────────────

    def run(self) -> None:
        try:
            self._session = BuySession().start()
            self._park(self.home, force=True)
            self._set("idle")
        except Exception as exc:
            print(f"[{_stamp()}] warm buying browser could not start: "
                  f"{type(exc).__name__}: {exc} — cold starts will be used")
            self._set("dead")
            self._ready.set()
            return
        finally:
            self._ready.set()

        last_refresh = time.monotonic()
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=5.0)
            except queue.Empty:
                # Keep the parked page fresh, but ONLY while genuinely idle.
                # A reload while holding is precisely what throws the basket
                # away, which is the most expensive thing this class can do.
                if (self.state == "idle"
                        and time.monotonic() - last_refresh > self.REFRESH_MINUTES * 60):
                    self._safe_park()
                    last_refresh = time.monotonic()
                continue

            kind = job[0]
            if kind == "stop":
                break
            if kind == "release":
                # The hold is over — David paid, or it lapsed. Navigating away
                # is what actually drops whatever is left in the basket.
                if self.state == "holding":
                    self._safe_park()
                    self._set("idle")
                continue
            if kind == "secure":
                self._run_job(job)
                last_refresh = time.monotonic()

        if self.state != "holding" and self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def _park(self, event, force: bool = False) -> None:
        if self._session is None:
            return
        if not force and self._parked_on == event.slug:
            return
        self._session.page.goto(event.url, wait_until="domcontentloaded")
        self._parked_on = event.slug
        self._prearm()

    def _prearm(self) -> None:
        """Set the quantity now, while nothing is waiting on it.

        The page loads with a quantity of 2 and resale results are filtered by
        quantity, so every attempt has to set it to 1 before it can search.
        Doing that on the critical path cost 1.6s and 6.5s on the two attempts
        of 2026-08-20 — the variation is the stepper rendering after a fresh
        page load, which is exactly the work that does not need a listing to
        be happening.

        Set here instead, on the parked page, where the seconds are free. The
        attempt still calls set_quantity, which now finds the value already
        correct and returns on its fast path rather than driving the stepper.

        Costs no request: the stepper is a client-side control and nothing is
        submitted until search is pressed.

        Never raises. A page that will not take a quantity here is a page the
        attempt will have to deal with itself, and a parked browser failing
        loudly over a preparation step would take out the warm browser for a
        problem that has not happened yet.
        """
        try:
            self._session.set_quantity(config.WANTED_QUANTITY, _ParkNotes())
        except Exception as exc:
            print(f"[{_stamp()}] warm browser could not pre-set the quantity: "
                  f"{type(exc).__name__}: {exc}")

    def _safe_park(self) -> None:
        """Re-park, swallowing anything. Used off the critical path only."""
        was = self.state
        try:
            self._set("busy")
            self._park(self.home, force=True)
        except Exception as exc:
            print(f"[{_stamp()}] warm browser could not re-park: {exc}")
        finally:
            self._set("idle" if was != "dead" else "dead")

    def _run_job(self, job) -> None:
        _, event, listing, may_preempt, box, done = job
        result = box["result"]
        try:
            if self.state == "holding":
                # Only reachable when the caller granted preemption; submit()
                # refuses otherwise. Navigating away drops the basket, which
                # is the whole meaning of preempting.
                result.note("dropping the live hold to go for a more "
                            "important ticket")
                self._park(self.home, force=True)
                result.preempted = True
            self._set("busy")
            if self._parked_on == event.slug:
                result.note("buying browser was already warm on this page")
                result.mark("warm")
            else:
                self._park(event)
                result.mark("navigate")
            box["result"] = secure(self._session, event, listing, result)
        except Exception as exc:
            result.reason = f"{type(exc).__name__}: {exc}"
            result.note(f"warm secure attempt failed: {result.reason}")
            box["result"] = result
        finally:
            if box["result"].secured:
                # The browser IS the checkout window now. Nothing may reuse,
                # reload or close it until the hold is done with.
                self._set("holding")
            else:
                self._safe_park()
            done.set()

    # ── the API, which only ever enqueues ────────────────────────────────

    def wait_until_ready(self, timeout: float = None) -> bool:
        self._ready.wait(self.STARTUP_TIMEOUT if timeout is None else timeout)
        return self.available

    def submit(self, event, listing, result, timeout_s: float, may_preempt=False):
        """Hand one attempt over. HoldResult, or None meaning 'not mine'.

        None is an instruction to the caller: fall back to a cold start. A
        definite refusal comes back as a HoldResult with a reason, because
        "the browser is holding something more important" is an answer David
        needs to read, not a reason to open a second browser on the same
        profile while a basket is live.
        """
        state = self.state
        if state in ("dead", "starting") or not self.is_alive():
            return None
        if state == "busy":
            # Answer, rather than sending the caller to the cold path.
            #
            # None means "not mine, open your own browser". For a dead or
            # still-starting worker that is right. For a BUSY one it is
            # actively wrong, and the night of 2026-08-24 is what it costs: a
            # cold start on a busy worker always finds the warm browser
            # holding the profile lock, and reports it as "already open
            # holding something at least as important as this" — which is a
            # sentence about a live basket, printed about a browser that is
            # merely mid-attempt and holding nothing.
            #
            # David reads those messages to decide whether to go and finish a
            # checkout by hand. Telling him a ticket is being held when none
            # is, is worse than saying nothing, and it repeated for six
            # consecutive listings while the real state was "busy".
            result.reason = (
                "the buying browser was already mid-attempt on another "
                "listing, so this one was not tried. Nothing is being held "
                "— it was busy, not holding."
            )
            result.note(result.reason)
            return result
        if state == "holding" and not may_preempt:
            result.reason = (
                "the buying browser is already open holding something at "
                "least as important as this, so it was left alone. Finish "
                "or close that window and this page can be secured by hand; "
                "nothing was touched here."
            )
            result.note(result.reason)
            return result

        box = {"result": result}
        done = threading.Event()
        self._jobs.put(("secure", event, listing, may_preempt, box, done))
        if not done.wait(timeout_s):
            result.reason = (
                f"the warm buying browser was still working after "
                f"{timeout_s:.0f}s and was abandoned — the poll loop must "
                f"not wait on it"
            )
            return result
        return box["result"]

    def release(self) -> None:
        """The hold is finished with. Ask the worker to free the browser."""
        if self.state == "holding":
            self._jobs.put(("release",))

    def shutdown(self) -> None:
        self._stop.set()
        self._jobs.put(("stop",))


def secure_in_thread(event, listing, timeout_s: int = None,
                     may_preempt: bool = False, worker=None) -> HoldResult:
    """Open the buying browser and hold `listing`, from its own thread.

    The thread is not an optimisation, it is the only way this works.
    Playwright's sync API refuses to start a second instance in a thread that
    already has an asyncio loop running, and the watcher's own browser has one
    running for the whole life of the process. Every securing attempt on
    2026-08-19 therefore died before it opened anything:

        Error: It looks like you are using Playwright Sync API inside the
        asyncio loop. Please use the Async API instead.

    Three real listings were found that afternoon — two Early Entry passes at
    €46.50 and a Weekend Camping at €366.39 — and all three produced a
    perfect availability alert followed by that message. The watcher was never
    going to hold anything, and no offline test could have caught it, because
    the fault only exists when a second Playwright starts inside a live one.

    A fresh thread has no event loop of its own, so sync_playwright() starts
    cleanly there. The thread is given a hard deadline and is left to die on
    its own if it overruns: a hung browser must not wedge the poll loop, which
    is the one thing that must keep running whatever else breaks.
    """
    from .state import stamp as state_stamp

    budget = timeout_s or (config.secure_budget_seconds() + 60)

    # The warm path, when there is one. See BuyerWorker: the browser is
    # already open and already on the page, so the attempt starts at the
    # search rather than at a cold Chrome launch and a 401-reload dance —
    # which together were most of the sixty seconds these listings do not
    # give us.
    #
    # A None back means "not mine, use the cold path": dead, still starting,
    # or busy with another attempt. A HoldResult back is a real answer,
    # including a refusal, and must be returned rather than retried cold —
    # opening a second browser on the same profile while a basket is live is
    # how a caught ticket gets thrown away.
    if worker is not None:
        warm = worker.submit(event, listing, HoldResult(), budget,
                             may_preempt=may_preempt)
        if warm is not None:
            line = warm.timing_line()
            if line:
                print(f"[{state_stamp()}] hold timings (warm): {line}")
            return warm

    box = {"result": HoldResult()}

    def run():
        session, hold = None, HoldResult()
        # Before opening anything. A buying browser that is already up is
        # almost always the previous hold still waiting to be paid for, and
        # the right answer is to say so — not to fail on a profile lock, and
        # certainly not to close the old window, which would drop a ticket
        # that is already caught in order to chase one that is not.
        if profile_in_use():
            if not may_preempt:
                hold.reason = (
                    "the buying browser is already open holding something at "
                    "least as important as this, so it was left alone. Finish "
                    "or close that window and this page can be secured by "
                    "hand; nothing was touched here."
                )
                hold.note(hold.reason)
                box["result"] = hold
                return
            # Outranks what is being held. Let go of it and take this instead.
            hold.note("a more important ticket than the one being held — "
                      "releasing the buying browser to go for this one")
            if release_buying_browser():
                hold.preempted = True
                hold.note("the earlier hold has been dropped")
        try:
            session = BuySession().start()
            hold.mark("launch")
            hold = secure(session, event, listing, hold)
        except Exception as exc:
            hold.reason = f"{type(exc).__name__}: {exc}"
            hold.note(f"secure attempt failed to start: {hold.reason}")
        finally:
            # Left OPEN on success: closing the browser is what drops the
            # basket, and the whole point is that David walks to this window
            # and pays in it. Closed on failure, because a signed-in Chrome
            # nobody is going to use is just an idle session to fingerprint.
            if session is not None and not hold.secured:
                try:
                    session.close()
                except Exception:
                    pass
            # Where the seconds went, win or lose. On a loss this is the whole
            # diagnosis: these listings are consumed in well under a minute,
            # so whichever step ate the most of it is the only thing worth
            # arguing about afterwards.
            line = hold.timing_line()
            if line:
                print(f"[{state_stamp()}] hold timings: {line}")
            box["result"] = hold

    worker = threading.Thread(target=run, name="ep-secure", daemon=True)
    worker.start()
    worker.join(timeout=budget)
    if worker.is_alive():
        box["result"].reason = (
            f"the securing browser was still working after {budget}s and was "
            f"abandoned — the poll loop must not wait on it"
        )
    return box["result"]


def secure(session: BuySession, event, listing, result: HoldResult = None) -> HoldResult:
    """Put `listing` in a basket, and try again if it was never takeable.

    One attempt is _secure_once below. This adds the only retry that is worth
    making, and refuses the one that is not.

    The distinction comes from the probe on the dead-end screen. When
    Ticketmaster says "sold or removed" and its own resale feed AGREES the
    ticket is gone, it sold: going back is pointless and would only spend
    requests against a rate limit that has already blocked this connection.
    That case returns immediately, exactly as before.

    But when the feed still lists the ticket a second after refusing it,
    nothing was sold. Something is holding it — most likely another buyer's
    basket — and those lapse. That is the case worth waiting out, and it is
    the case a faster watcher could never have won, because there was nothing
    to win at the moment it looked.

    Bounded by the same SECURE_TIMEOUT_SECONDS budget the single attempt
    always had, so this cannot hold the buying browser — or the poll loop
    behind it — any longer than it could before. A weekend ticket can still
    preempt the whole thing.
    """
    result = result or HoldResult()
    start = time.monotonic()
    deadline = start + config.SECURE_TIMEOUT_SECONDS
    #: Blocks are counted separately from ordinary retries, so a challenge
    #: cannot quietly spend the whole budget meant for waiting out a basket.
    challenges = 0

    def verdict(out):
        """Correct the reason against what the EARLIER attempts learned.

        Without this the record carries the message the LAST look earns on its
        own — "the race being lost at the last step, not a fault in the
        watcher" — about a listing an earlier look had already proved was
        unsold and merely unavailable. Seen twice on 2026-08-21, at 12:20 and
        12:25, in exactly that shape.

        The distinction decides where the next day's work goes. "We lost a
        race" points at shaving seconds off the click. "It was claimed before
        we saw it" points at seeing it sooner, and says the click was never
        going to succeed however fast it was.
        """
        # Ticketmaster's own word first. When the error payload called this
        # listing active, "it sold" is not a conclusion available to us — it
        # is contradicted by the party that would know, at the moment of
        # refusal. Saying it anyway is how the log came to file fifteen
        # refusals of live tickets as lost races, which pointed a fortnight of
        # work at shaving seconds off a click that was never the problem.
        if out.ever_active and not out.secured:
            # Say what was observed; stop naming a cause.
            #
            # This message used to end "most likely sitting in somebody else's
            # basket". That was an inference, written as a finding, and five
            # days of data have now contradicted it: a basket hold would show
            # the listing dropping out of the feed and returning, and across
            # fourteen visits to l0vmtvwkd2 it never left. It also sent the
            # chase off to wait twelve minutes for a lapse that never came.
            #
            # The pattern this project keeps repeating is not a wrong guess,
            # it is a guess recorded as a fact — "we lost the race", "it was
            # in a basket", "we asked for zero", each written into the log as
            # settled and each overturned by the next day's evidence. What is
            # actually known here is exactly two things: Ticketmaster refused
            # us, and Ticketmaster said the listing was live. The reason for
            # the refusal is not in evidence, and the honest record says so.
            out.reason = active_refusal_reason(out)
            out.note(out.reason)
            return out
        if out.ever_listed_after and not out.secured and not out.still_listed_after:
            out.reason = (
                "this was not a race lost at the click. An earlier attempt was "
                "refused while Ticketmaster's own feed still listed the very "
                "same ticket — so it had not sold, it was already sitting in "
                "somebody else's basket. By the time we came back they had "
                "paid for it. Being faster at the last step would have changed "
                "nothing; the listing was claimed before we ever saw it."
            )
            out.note(out.reason)
        return out

    # The widest of the ceilings, because the loop bound must not be the thing
    # that stops a chase. Which ceiling actually applies is decided per attempt
    # below, against what that attempt learned — a listing Ticketmaster calls
    # active is worth more goes than one the feed merely still shows. Capping
    # the range at SECURE_RETRIES instead made the active limit unreachable
    # and silently reinstated the shorter chase.
    for attempt in range(1 + max(config.SECURE_RETRIES,
                                 config.SECURE_ACTIVE_RETRIES)):
        if attempt:
            result.attempts = attempt + 1
            result.note(f"attempt {attempt + 1}: going back for it")
        out = _secure_once(session, event, listing, result, deadline)
        if out.secured:
            return out

        # A block is worth waiting out, and used to end the attempt at once.
        #
        # Ticketmaster showed the buying browser "Your Browsing Activity Has
        # Been Paused" three times on 2026-08-22. Each of those failed before
        # the resale panel, so still_listed_after was never set and the branch
        # below read it as "nothing to come back for" — one try, fifty seconds,
        # done. But a challenge is the client being asked to wait, not the
        # ticket being gone; the listing may well still be there.
        #
        # Waited out more patiently and fewer times than an ordinary retry.
        # Hammering a challenge screen is exactly what turns a pause into a
        # ban, and this project has been blocked twenty-two times already.
        if out.challenged:
            # Waiting it out was worth one try. It is not worth many: the
            # block of 2026-08-23 held from 10:21 to 19:00, through fourteen
            # attempts, and every extra go is both a wasted minute of the poll
            # loop and another knock on a door that has been shut.
            if challenges >= config.SECURE_CHALLENGE_RETRIES:
                out.note("still blocked after "
                         f"{challenges + 1} tries — leaving it alone rather "
                         f"than provoking it further")
                return verdict(out)
            pause = config.SECURE_CHALLENGE_PAUSE_SECONDS
            if time.monotonic() + pause >= deadline:
                out.note("blocked, and no time left in the window to wait it "
                         "out — the alert tells David to buy it himself")
                return verdict(out)
            challenges += 1
            out.note(f"blocked by a challenge screen — waiting {pause:.0f}s "
                     f"for it to clear rather than retrying straight away")
            out.challenged = False
            time.sleep(pause)
            out.mark("waiting")
            continue

        # Is there anything to come back for?
        #
        # Two independent reasons to say yes, and the second is the one that
        # was missing. The feed can only answer "is this offerable to me right
        # now", so a ticket sitting in somebody's basket drops out of it and
        # reads here as sold. Ticketmaster's own error page answers the
        # question the retry actually turns on — does the listing still exist
        # — and it said yes on all fifteen refusals captured to 2026-08-24,
        # including the ones this loop then abandoned as gone.
        if out.listing_active:
            out.ever_active = True
            # Positive evidence the ticket exists buys a longer window, once.
            # Extended rather than replaced, so a chase already under way
            # keeps its remaining time instead of restarting it.
            grown = start + config.SECURE_ACTIVE_TIMEOUT_SECONDS
            if grown > deadline:
                out.note(
                    f"extending the window to "
                    f"{config.SECURE_ACTIVE_TIMEOUT_SECONDS // 60} min — "
                    f"Ticketmaster says this listing is still active, so there "
                    f"is something real to wait for"
                )
                deadline = grown
        # `ever_active` counts, not just this attempt's reading.
        #
        # The first live chases, on 2026-08-24 at 12:21 and 12:28, stopped
        # after six goes and four instead of the ten they were given. The
        # sequence explains itself: the opening refusal proves the listing is
        # active, the basket holding it then takes the listing out of the
        # resale feed, so the next attempt finds no row, never clicks, never
        # reaches an error page — and is judged on an empty feed and no error
        # payload, which reads exactly like "it sold".
        #
        # That is the one state this chase exists for. A ticket in somebody's
        # basket is INVISIBLE in the feed by definition; treating its absence
        # as proof of sale abandons the wait at the moment the wait is the
        # whole strategy. What was established minutes ago does not stop being
        # true because the last look could not re-establish it, so the memory
        # is what governs, bounded by the retry cap and the deadline below.
        worth_returning = (bool(out.still_listed_after)
                           or bool(out.listing_active)
                           or bool(out.ever_active))
        # Genuinely sold, or the question could not be asked either way.
        if not worth_returning:
            return verdict(out)
        limit = (config.SECURE_ACTIVE_RETRIES
                 if (out.listing_active or out.ever_active)
                 else config.SECURE_RETRIES)
        if attempt >= limit:
            out.note(f"still there, but {attempt + 1} attempts is the limit — "
                     f"the alert tells David to try it himself")
            return verdict(out)
        pause = config.SECURE_RETRY_PAUSE_SECONDS
        if time.monotonic() + pause >= deadline:
            out.note("still there, but there is no time left in the window "
                     "to go back — the alert tells David to try it himself")
            return verdict(out)
        if out.listing_active or out.ever_active:
            out.note(f"the listing is still active, so it did not sell — "
                     f"waiting {pause:.0f}s for the basket holding it to lapse")
        else:
            out.note(f"it is still in the feed, so it did not sell — waiting "
                     f"{pause:.0f}s for the basket holding it to lapse")
        # Remembered across attempts before it is cleared. What the FIRST
        # probe saw is the fact that explains the whole sequence, and the last
        # attempt's answer overwrites it.
        out.ever_listed_after = True
        # Cleared so the next attempt's probe answers for itself. A stale True
        # here would be read as evidence from a look that never happened.
        out.still_listed_after = None
        out.ids_after = []
        # Same discipline for the error payload. `ever_active` above is what
        # remembers across attempts; this must answer for the next one only,
        # or a single active reading would keep the chase alive long after
        # Ticketmaster stopped saying so.
        out.listing_active = None
        # Watch the feed through the pause rather than sleeping blind. Costs
        # one XHR per check instead of a whole search, and returns the instant
        # the ticket comes back — see _wait_for_relist().
        came_back = _wait_for_relist(session, event, listing, out, pause, deadline)
        # Do not spend a search on a ticket the feed still says is held.
        #
        # A full attempt is a page load, a quantity set, a search and a panel
        # render — the request shape of a poll — and while the listing is in
        # somebody's basket it can only end one way: no row, no click, nothing
        # learned. The chases of 2026-08-24 spent four and six of those, which
        # is both the block risk this budget is trying to avoid and a pointless
        # fifteen seconds each time.
        #
        # So keep waiting instead, and go only when there is something to go
        # for. This costs no retries — the cap counts attempts, and waiting is
        # not one — so the shape of a chase becomes "watch cheaply until it
        # comes free, then pounce", bounded by the deadline rather than by how
        # many times we were willing to knock on a locked door.
        if not came_back and (out.listing_active or out.ever_active):
            # One further wait, for whatever is left of the window minus the
            # room an attempt needs. Deliberately a single sized call rather
            # than a loop around the short one: a loop whose only brake is the
            # clock spins the instant a sleep stops costing time, and
            # _wait_for_relist is bounded by its own poll count as well.
            left = deadline - time.monotonic() - pause
            if left > 0:
                came_back = _wait_for_relist(
                    session, event, listing, out, left, deadline)
        if not came_back:
            out.note("the window is up and it never came free — going back "
                     "once more to see the page for itself")
        # Charge the wait to itself.
        #
        # mark() measures the gap since the previous mark, and the previous
        # mark belongs to the attempt BEFORE this sleep — so without this the
        # whole pause lands on the next attempt's `navigate`, which is the
        # first step it marks. That is not a rounding error: it read as
        # "navigate 22.5s" with a twenty-second pause and "navigate 41.9s"
        # with a forty-second one, on a home connection where a single-attempt
        # navigate measures 0.0s. It was reported as evidence of a slow
        # connection for a day, and it was this.
        out.mark("waiting")

    return verdict(out)


def _wait_for_relist(session, event, listing, result: HoldResult,
                     pause_s: float, deadline: float) -> bool:
    """Wait out the basket holding a listing, watching the feed rather than the clock.

    Returns True when the listing came back and the caller should go NOW.

    This exists because the obvious version of the chase is dangerous. A full
    attempt costs a page load, a quantity set, a search and a panel render —
    the same request shape as a poll — and the active-listing window allows
    eleven of them in twelve minutes. That is roughly fifty-five searches an
    hour sustained, against a budget of 16.7 that is already deliberately
    under the twenty that first drew a block. Chasing a live ticket by
    hammering the search is how you turn one refusal into the block screen
    that caused half of all the refusals in the first place.

    So the waiting is done on the resale endpoint instead: one same-origin
    XHR, the identical call the sweep already makes every ninety seconds, from
    a page that is already open. A chase then costs one page load and a
    handful of XHRs per pause instead of a search per pause, and the expensive
    attempt is only spent when the feed says there is something to spend it
    on.

    It is also faster at the thing that matters. Sleeping a flat forty seconds
    means a basket that lapses one second after the sleep starts is not acted
    on for thirty-nine of them — and the whole premise of the chase is that
    the moment of lapse is contested. Polling turns that worst case into the
    poll interval.

    Falls back to sleeping when there is no usable session, so the retry loop
    keeps its old behaviour rather than losing a chase to a missing browser.
    """
    end = min(time.monotonic() + pause_s, deadline)
    wanted = (getattr(listing, "listing_id", "") or result.listing_id or "")

    page = None
    try:
        page = session.page if session is not None else None
    except Exception:
        page = None

    if page is None:
        remaining = end - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return False

    # Get back onto the event's own origin once, and only if we are not there.
    # The refusal leaves the browser on secure.ticketmaster.ie, where the
    # resale endpoint cannot be reached at all — the origin problem that has
    # kept the forensics blind on this exact path.
    try:
        here = (page.url or "").split("?")[0].rstrip("/")
        if here != event.url.split("?")[0].rstrip("/"):
            page.goto(event.url, wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            result.note("back on the event page to watch for the basket lapsing")
    except Exception as exc:
        result.note(f"could not get back to the event page to watch: "
                    f"{type(exc).__name__}")
        remaining = end - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return False

    # Bounded by a count as well as by the clock.
    #
    # The clock alone is not a bound. This loop's only brake is time passing,
    # and time passing here is `time.sleep` — so anything that makes a sleep
    # cheap (a stubbed clock, a suspended machine resuming, a sleep that
    # returns early) turns a patient wait into a spin that pegs a core and
    # never checks out. A ceiling on looks costs nothing when the clock
    # behaves and is the difference between a wait and a hang when it does
    # not.
    polls = 0
    looks = 0
    budget = max(1, int(pause_s / max(config.SECURE_RELIST_POLL_SECONDS, 0.001)) + 1)
    # `looks` counts iterations and `polls` counts answers, and the ceiling is
    # on the former. Counting only answers would leave the loop unbounded on
    # exactly the failure it is most likely to meet — a feed that keeps
    # replying with something unreadable, which `continue`s without ever
    # incrementing an answer count.
    while time.monotonic() < end and looks < budget:
        looks += 1
        nap = min(config.SECURE_RELIST_POLL_SECONDS, end - time.monotonic())
        if nap > 0:
            time.sleep(nap)
        if time.monotonic() >= deadline:
            break
        try:
            record = session.listings_now(event, config.WANTED_QUANTITY)
            data = (record or {}).get("data")
            if not isinstance(data, dict):
                continue
            polls += 1
            picks = data.get("picks")
            picks = picks if isinstance(picks, list) else []
            ids = [str(p.get("resaleListingId") or p.get("id"))
                   for p in picks if isinstance(p, dict)
                   and (p.get("resaleListingId") or p.get("id"))]
            # The one we were refused, or — failing that — anything at all.
            # The ids have been observed to change between polls for what is
            # plainly the same ticket, so an exact match is the strong signal
            # and any listing at all is the weak one worth acting on.
            if wanted and wanted in ids:
                result.note(f"the listing is back in the feed after {polls} "
                            f"check(s) — going for it now")
                return True
            if ids and not wanted:
                result.note(f"a listing is back in the feed after {polls} "
                            f"check(s) — going for it now")
                return True
        except Exception:
            # A failed check is not a reason to abandon the chase; it is one
            # missed look out of many, and the pause continues.
            continue
    if polls:
        result.note(f"still held after {polls} check(s) of the feed — "
                    f"going back anyway")
    return False


def _secure_once(session: BuySession, event, listing,
                 result: HoldResult = None, deadline: float = None) -> HoldResult:
    """One attempt at putting `listing` in a basket. Returns without paying, always.

    `session` must already be started and signed in. Failure at any step is
    recorded and returned rather than raised: the caller's next move is to
    send the ordinary "a ticket is live" alert, which must not be lost
    because this optimistic extra step went wrong.

    `deadline` is a monotonic clock shared with secure() above, so that
    retries spend one budget between them rather than a fresh one each.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    from .sources.browser import BASKET_MARKERS, SEARCH_BUTTONS

    result = result or HoldResult()
    if deadline is None:
        deadline = time.monotonic() + config.SECURE_TIMEOUT_SECONDS

    def out_of_time() -> bool:
        if time.monotonic() < deadline:
            return False
        result.reason = (
            f"gave up after {config.SECURE_TIMEOUT_SECONDS}s — the listing is "
            f"most likely in someone else's basket"
        )
        result.note(result.reason)
        return True

    # Bound before the try so the finally can always ask where the page ended
    # up, including when getting the page is itself what failed. The trace is
    # bound here for the same reason and a sharper one: it is READ in the
    # finally, so a failure before it was constructed would turn any error on
    # this path into a NameError raised from the cleanup — losing the real
    # exception and the attempt's own account of itself.
    page = None
    trace = OfferTrace()

    try:
        # Navigate BEFORE asking whether we are signed in. A freshly started
        # BrowserSession is parked on about:blank, which contains neither
        # "sign out" nor "my account" — so checking first meant the answer was
        # always "not signed in", and the whole feature was a no-op that would
        # have reported a login problem on the first real listing. Caught by
        # reading the flow back on 2026-08-19, before any listing tested it.
        page = session.page
        # Listen from here, so the trace covers the whole attempt rather than
        # only the click. What the page requests while searching is part of
        # the answer too — if the offer is refused before the click ever
        # happens, that shows up here and nowhere else.
        trace.attach(page)
        result.offer_ids = tuple(getattr(listing, "offer_ids", ()) or ())
        if result.offer_ids:
            result.note(f"offer id(s) from the feed: {', '.join(result.offer_ids)}")
        # Don't reload a page we are already standing on.
        #
        # The warm browser parks on the event page precisely so an attempt can
        # start at the search, and it reported "already warm on this page" on
        # both of the attempts on 2026-08-20 — and then spent a second
        # reloading that same page anyway, because this goto was
        # unconditional. The warm path saved the cold start and nothing else.
        #
        # Compared on the path alone. The parked URL can carry query
        # parameters the event URL does not, and a string mismatch here would
        # silently restore the reload while still reporting "warm".
        here = ""
        try:
            here = (page.url or "").split("?")[0].rstrip("/")
        except Exception:
            here = ""
        if here and here == event.url.split("?")[0].rstrip("/"):
            result.note(f"already on {event.slug} — starting at the search")
        else:
            page.goto(event.url, wait_until="domcontentloaded")
            result.note(f"opened {event.slug} in the buying browser")
        result.mark("navigate")

        # Note it, do not refuse on it.
        #
        # This used to return here when the session did not look signed in.
        # That gate was removed on 2026-08-19 once the detection behind it was
        # shown to be unreliable in the dangerous direction: Ticketmaster
        # renders no account text that Playwright's flattened inner_text can
        # read, so a perfectly good session reads as signed out. Refusing on
        # it would have thrown away the first real listing after David signed
        # in correctly.
        #
        # Trying anyway costs nothing that matters. A signed-out attempt
        # bounces off a login wall, holds nothing, and reports honestly — the
        # same outcome as refusing, minus the chance of being wrong about it.
        # The availability alert has already gone out either way.
        evidence = session_evidence()
        if evidence["signed_in"] is False:
            result.note(f"the buying session looks signed out ({evidence['reason']}) "
                        f"— trying anyway, since that reading can be wrong")
        elif evidence["signed_in"] is None:
            result.note("cannot tell whether the buying session is signed in — trying")

        # Is this the event page at all? Asked FIRST, and cheaply.
        #
        # A block screen has no stepper and no search button, so the attempt
        # discovered it the slow way: ten seconds timing out on the quantity
        # control, then fifteen on the search button, a reload, and fifteen
        # more. With the challenge retry on top that came to 271 seconds per
        # attempt — and on 2026-08-23 fourteen consecutive finds were blocked,
        # which cost about an hour of the poll loop, because submit() blocks
        # the caller for the whole attempt.
        #
        # Two page reads answer it in well under a second.
        hit = challenge_markers(page)
        if hit:
            result.challenged = True
            result.reason = (
                f"Ticketmaster is showing the buying browser a block screen "
                f"rather than the event page ({hit[0]}). The watcher cannot "
                f"reach any listing until this clears. Buy it by hand from "
                f"the link in the availability email."
            )
            result.note(result.reason)
            return result

        # Go straight to the offer, if we know which listing we are after.
        #
        # This is the fix for the reason nothing has ever been held, and the
        # shortcut is the smaller half of it — see offer_url(). Clicking the
        # row makes the page build its own link, and the page has built that
        # link with qty=0 on all eighteen attempts ever traced, which
        # Ticketmaster redirects to the "sold or removed" screen. Constructing
        # it here is what puts a 1 in the request.
        #
        # It also skips the quantity, the search and the panel wait — about
        # twenty of the twenty-two seconds an attempt spends — which matters
        # much less than it once seemed to, because a malformed request was
        # never going to be rescued by arriving sooner.
        #
        # The old path stays underneath as the fallback. It has never secured
        # anything, but it is the one that has met every shape this page can
        # take, and a direct URL that turns out to be refused for some other
        # reason must not leave the attempt with nowhere to go.
        wanted_id = (getattr(listing, "listing_id", "") or "")
        direct = offer_url(event, wanted_id) if config.DIRECT_OFFER else ""
        if direct:
            # Attempt one goes out exactly as Ticketmaster's own page builds
            # it. A retry gets a nonce, because without one Chrome answers it
            # from disk in a millisecond and the "refusal" is a copy of the
            # first. See uncached_offer_url().
            direct = uncached_offer_url(direct, getattr(result, "attempts", 1))
            result.note(f"going straight to the offer: {direct}")
            try:
                _t0 = time.monotonic()
                page.goto(direct, wait_until="domcontentloaded",
                          timeout=config.PAGE_TIMEOUT_MS)
                _note_if_cached(result, time.monotonic() - _t0)
                result.used_direct = True
                result.mark("direct")
            except (PlaywrightTimeout, PlaywrightError) as exc:
                result.note(f"the direct offer link would not load ({type(exc).__name__})"
                            f" — falling back to the search")
                direct = ""
        if direct:
            # Carry on only with positive evidence of where we landed.
            #
            # A basket is the win. The listing's own page is a good sign and
            # the loop below finishes the job. ANYTHING ELSE falls back — the
            # refusal screens, and equally a page this does not recognise at
            # all, because "the URL loaded" is not "the URL worked" and a
            # silent unknown is how a shortcut turns into a dead end with no
            # second chance. The search path is slower and has met every shape
            # this site can take; it is the right thing to be behind us.
            if _basket_is_live(page, BASKET_MARKERS):
                result.note("the direct offer link went straight to a basket")
            elif _page_says(page, LISTING_DETAIL_MARKERS, all_of=True):
                result.note("the direct offer link reached the listing's own page")
            else:
                if _page_says(page, LISTING_GONE_MARKERS) or challenge_markers(page):
                    result.note("the direct offer link hit a refusal — falling "
                                "back to the search, which knows this screen")
                else:
                    result.note("the direct offer link landed somewhere "
                                "unrecognised — falling back to the search")
                direct = ""
                try:
                    page.goto(event.url, wait_until="domcontentloaded",
                              timeout=config.PAGE_TIMEOUT_MS)
                except (PlaywrightTimeout, PlaywrightError):
                    pass

        if not direct:
            # Same quantity discipline as the watcher: the page defaults to 2 and
            # resale results are filtered by quantity, so asking for the wrong
            # number manufactures a refusal against a listing that is really there.
            session.set_quantity(config.WANTED_QUANTITY, result)
            result.mark("quantity")

            # Press search, and if the button is not there, reload once and press
            # again.
            #
            # The retry is not defensive padding. "Waiting for the search button
            # to be visible" timing out is the single most common browser failure
            # this project has — thirteen occurrences in the log — and on the
            # watching side it costs one poll out of hundreds, which is why it was
            # never worth handling there. On 2026-08-21 at 05:57 it happened HERE
            # instead, on a real weekend listing, and the attempt simply returned:
            # ten seconds setting a quantity, fifteen more waiting for a button,
            # and a ticket lost to a page that had gone stale in a warm browser.
            #
            # A parked page is the likeliest cause and a reload is the obvious
            # answer to it, which is what makes the omission galling rather than
            # subtle. Bounded at one extra go and charged to the same deadline as
            # everything else, so a page that is genuinely broken still fails
            # inside the window instead of eating it.
            pressed = False
            for press_attempt in range(2):
                try:
                    page.get_by_role(
                        "button", name=SEARCH_BUTTONS).first.click(timeout=15_000)
                    pressed = True
                    break
                except (PlaywrightTimeout, PlaywrightError) as exc:
                    # Second go, or no time left to make one: report and stop.
                    # mark() first, so the seconds spent failing are attributed to
                    # the step that failed rather than vanishing from the record.
                    if press_attempt or out_of_time():
                        result.mark("search")
                        # WHY there was no button. A challenge screen and a slow
                        # page produce the identical Playwright timeout, and they
                        # call for opposite responses — one is worth waiting out,
                        # the other is not — so for three failures on 2026-08-22
                        # they were reported identically as "could not press
                        # search", which reads as a selector problem and is not.
                        hit = challenge_markers(page)
                        if hit:
                            result.challenged = True
                            result.reason = (
                                f"Ticketmaster is showing the buying browser a "
                                f"block screen rather than the event page "
                                f"({hit[0]}). This is not a selector problem and "
                                f"not a lost race — the watcher cannot reach the "
                                f"listing at all right now. Buy it by hand from "
                                f"the link in the availability email."
                            )
                        else:
                            result.reason = (
                                f"could not press search in the buying browser"
                                f"{' even after reloading' if press_attempt else ''}"
                                f": {exc}"
                            )
                        result.note(result.reason)
                        return result
                    result.note("the search button never became clickable — "
                                "reloading the page and trying once more")
                    try:
                        page.goto(event.url, wait_until="domcontentloaded")
                        # The reload resets the stepper to the page default of 2,
                        # and searching for the wrong number manufactures a
                        # refusal against a listing that is really there.
                        session.set_quantity(config.WANTED_QUANTITY, result)
                    except (PlaywrightTimeout, PlaywrightError) as reload_exc:
                        result.mark("search")
                        result.reason = (
                            f"the search button never appeared and the page could "
                            f"not be reloaded either: {reload_exc}"
                        )
                        result.note(result.reason)
                        return result
            result.mark("search")
            if pressed:
                result.note(f"searched for {config.WANTED_QUANTITY}")

            if out_of_time():
                return result

            # Wait for the panel before looking for anything in it. Pressing
            # search does not produce listings — a separate call has to answer and
            # the panel has to paint. Skipping this is why the first three real
            # attempts all reported the listing as gone. See await_listings().
            session.await_listings(result, budget_s=max(5.0, deadline - time.monotonic()))
            result.mark("panel")

            if out_of_time():
                return result

            # Find the listing row. Matched on the section rather than on the
            # listing id, because the id is an API field and has never been seen
            # in the rendered page — and section plus price is what distinguishes
            # one row from another when several are live.
            row = _find_listing_row(page, listing, result)
            result.mark("find_row")
            if row is None:
                # Do not guess at why. "Gone" and "not drawn" call for opposite
                # responses — one means the race was lost and nothing can be done,
                # the other means this code looked too early and is fixable — and
                # for three attempts they were reported identically, as the former.
                # The endpoint that the panel is a drawing of can tell them apart.
                #
                # Asked through _probe_after_gone rather than by a second reader
                # written inline here, which is what this used to be. The inline
                # version had two faults, and both were invisible because they
                # only showed on the losing path.
                #
                # It read `data["picks"] or data["listings"]` where the probe
                # reads `picks` alone — two answers to one question about one
                # payload, one of which must be wrong. And it kept its finding in
                # a local variable, so `still_listed_after` stayed None even when
                # the feed had given a definite answer. That is not merely a gap
                # in the record: secure() decides whether to go back by reading
                # that field, so the branch below that concludes the ticket was
                # THERE — the one case a retry can win — returned instead of
                # retrying, every time. Fourteen attempts, and the retry has never
                # once fired.
                _probe_after_gone(session, event, listing, result, page)
                if result.still_listed_after:
                    result.reason = (
                        f"the resale endpoint still shows "
                        f"{len(result.ids_after) or 'some'} listing(s), but no row "
                        f"for them could be found on the page. That is a rendering "
                        f"or selector problem in the buying browser, not a lost "
                        f"race — the ticket was there and reachable by hand."
                    )
                elif result.still_listed_after is False:
                    # "Sold" is one explanation, not the only one, and this
                    # cannot tell them apart. The feed also goes empty when a
                    # listing is withdrawn, and — on the evidence of 49
                    # consecutive refusals — when Ticketmaster simply stops
                    # offering it to this browser. Claiming a sale here is
                    # what filed live tickets as lost races for a fortnight
                    # and pointed the work at speed.
                    result.reason = (
                        "the listing is no longer being offered to us — the "
                        "page refused it and the resale endpoint now reports "
                        "nothing left. Most likely sold; possibly withdrawn. "
                        "Not distinguishable from here."
                    )
                else:
                    result.reason = (
                        "the listing could not be found on the page, and the resale "
                        "endpoint could not be asked either, so whether it sold or "
                        "simply never rendered is unknown"
                    )
                result.note(result.reason)
                return result

            # What does this row actually point at?
            #
            # Recorded before the click, because after it the element is gone and
            # the only evidence left is the error page it landed on. If the row is
            # an anchor, its href IS the direct path the whole project has been
            # guessing at; if it is a scripted div, that is worth knowing too,
            # because it means no URL exists to shortcut to.
            try:
                for attr in ("href", "data-href", "data-url", "data-offer-id",
                             "data-listing-id", "id"):
                    value = row.get_attribute(attr)
                    if value:
                        result.note(f"row {attr}={value[:200]}")
                        if attr in ("href", "data-href", "data-url"):
                            result.row_href = value
            except Exception:
                pass

            try:
                row.click(timeout=10_000)
                result.mark("click")
                result.note("clicked into the listing")
            except (PlaywrightTimeout, PlaywrightError) as exc:
                result.reason = f"could not click the listing: {exc}"
                result.note(result.reason)
                return result

        # Then follow the flow only as far as a basket. Each of these is
        # optional — Ticketmaster's resale path has varied — and none of them
        # is a payment control. The allowlist is what guarantees that: a
        # button whose name is not in it is never pressed, so a future page
        # that puts "Place Order" where "Continue" used to be cannot be
        # clicked by accident.
        reached_detail = False
        for _ in range(4):
            if out_of_time():
                return result
            if _basket_is_live(page, BASKET_MARKERS):
                break
            # Did we get as far as the listing's own page? Worth recording
            # even when the answer that follows is bad news, because "never
            # reached the listing" and "reached it and it was gone" need
            # different fixes and used to read identically in the email.
            if not reached_detail and _page_says(page, LISTING_DETAIL_MARKERS, all_of=True):
                reached_detail = True
                result.note("reached the listing's own page — the click-through works")
            # A definite no. Stop at once rather than spending what is left of
            # the window pressing buttons at a dead end; the only control on
            # this screen is "Find More Tickets", which would restart the
            # search and lose the page we are on.
            if _page_says(page, LISTING_GONE_MARKERS):
                # Ask the feed before believing the page. See the forensics
                # fields on HoldResult: "sold" and "held by somebody else"
                # produce this identical screen and call for opposite
                # responses, and the difference is one call away.
                _probe_after_gone(session, event, listing, result, page)
                if result.still_listed_after:
                    result.reason = (
                        "Ticketmaster showed the 'sold or removed' page, but "
                        "the resale feed STILL lists this ticket a second "
                        "later. That is not a race we lost by being slow — it "
                        "is a listing that cannot be taken right now, most "
                        "likely sitting in somebody else's basket. Those "
                        "expire, so it is worth trying again in a few minutes."
                    )
                elif result.still_listed_after is False:
                    result.reason = (
                        "the listing was gone by the time we clicked into it — "
                        "Ticketmaster says it has been sold or withdrawn, and "
                        "the resale feed agrees it is no longer there. This is "
                        "the race being lost at the last step, not a fault in "
                        "the watcher."
                    )
                else:
                    # The feed could not be asked, which is NOT the same as the
                    # feed agreeing, and this branch used to claim the second
                    # when only the first was true.
                    #
                    # It is the ordinary case rather than a rare one, and the
                    # live attempt of 2026-08-21 11:19 is what showed it. The
                    # dead end is served from secure.ticketmaster.ie while the
                    # resale endpoint is same-origin to www.ticketmaster.ie —
                    # so the probe's relative fetch has the wrong origin to
                    # resolve against and cannot answer at all. Every attempt
                    # that reaches this screen is in that position.
                    #
                    # Saying "the feed agrees it is gone" from there invents a
                    # confirmation. It is the exact conflation this project
                    # refuses everywhere else: "it sold" and "we could not
                    # tell" call for different responses, and reporting the
                    # first when the second is true is how a fixable problem
                    # gets filed as bad luck.
                    result.reason = (
                        "the listing was gone by the time we clicked into it — "
                        "Ticketmaster says it has been sold or withdrawn. The "
                        "resale feed could NOT be asked to confirm that, "
                        "because the dead end is served from a different "
                        "origin than the endpoint, so whether it truly sold or "
                        "was merely untakeable is unknown."
                    )
                result.note(result.reason)
                return result
            if not _press_one_safe_button(page, result):
                break
            time.sleep(1.5)

        if _basket_is_live(page, BASKET_MARKERS):
            result.secured = True
            # Prefer the clock on the page over the configured guess. The
            # guess comes from one observation of a different event, and a
            # number telling David how long he has should be measured when it
            # can be.
            seen = read_countdown_minutes(page)
            if seen is not None:
                result.minutes_hint = int(seen)
                result.minutes_measured = True
                result.note(f"countdown read from the page: {seen:.1f} min")
            else:
                result.minutes_hint = config.HOLD_MINUTES_HINT
                result.note("no countdown visible — using the configured estimate")
            result.note("CHECKOUT REACHED — this is as far as anything has "
                        "ever got")

            # Take the one step that turns a reachable ticket into a reserved
            # one, then stop for good. See _reserve_at_checkout().
            _reserve_at_checkout(page, result)

            # Where the checkout actually is, captured at the moment it exists.
            #
            # This alert deliberately carried no link, on the reasoning that a
            # basket lives in the session that created it and a link opened on
            # a phone would be an empty checkout while the real hold expired.
            # That reasoning is certainly right for a signed-OUT session and
            # may be wrong for a signed-in one: a cart bound to the ACCOUNT
            # server-side would follow David to any device he is signed in on.
            # Nobody has tested which it is here.
            #
            # So the URL is captured and offered, described as worth trying
            # rather than as the answer. Offering it costs nothing if the cart
            # does not travel — he sees an empty basket and walks to the
            # laptop, which is exactly what he would have done without it.
            # Withholding it costs the ticket on every occasion he is out and
            # it would have worked.
            result.mark("basket")
            try:
                result.checkout_url = page.url
                result.note(f"checkout URL captured: {result.checkout_url}")
            except Exception:
                pass

            # Bring it to the front so the machine he walks to is already
            # showing the thing he has to finish.
            try:
                page.bring_to_front()
            except Exception:
                pass
            return result

        if _page_says(page, LISTING_GONE_MARKERS):
            result.reason = (
                "the listing was gone by the time we clicked into it — "
                "Ticketmaster says it has been sold or withdrawn."
            )
        elif reached_detail:
            result.reason = (
                "reached the listing's own page but no basket appeared. The "
                "click-through works; it is the step after it that did not. "
                "The page text is recorded in the log."
            )
        else:
            result.reason = (
                "never reached the listing's own page — the row could not be "
                "clicked, or the page did not respond to it"
            )
        result.note(result.reason)
        return result

    except Exception as exc:
        # Never let this cost the ordinary alert. Whatever happened, David
        # still needs to be told a ticket existed.
        result.reason = f"{type(exc).__name__}: {exc}"
        result.note(f"secure attempt failed — {result.reason}")
        return result

    finally:
        # Two things that must be true however this attempt ended, including
        # the paths that end by raising.
        #
        # Where the page finished. _probe_after_gone records this too, but it
        # runs on exactly one of the five ways an attempt can fail — and both
        # attempts recorded since the field was added failed before reaching
        # it, so the field it exists to fill has been "" for its whole life.
        # That matters more than it sounds: the dead end's URL is the only
        # place a direct link to a single listing has ever been visible, and a
        # direct link is what would let a later attempt skip the
        # navigate-quantity-search-panel sequence that costs three quarters of
        # every attempt. Capturing it on one path in five is how a question
        # stays open for want of a single line.
        #
        # And when it stopped, so `elapsed` reports the real duration rather
        # than continuing to run while the failure email is written.
        if page is not None:
            try:
                result.landed_url = (page.url or result.landed_url)
            except Exception:
                pass
        # Harvest the trace and stop listening. Detaching matters: the buying
        # browser is warm and long-lived, so a listener left attached would
        # accumulate across every attempt for the life of the process and
        # attribute one attempt's traffic to the next.
        try:
            result.trace = trace.summary()
            trace.detach()
        except Exception:
            pass
        result.finished_at = time.monotonic()
        # And write down how it failed, while the page that failed is still on
        # screen. This is the only moment the evidence exists: the warm
        # browser re-parks itself the instant the attempt returns, and every
        # attempt before 2026-08-21 was reconstructed afterwards from prose.
        if page is not None and not result.secured:
            try:
                capture_failure(page, result, event, result.attempts)
            except Exception as exc:
                print(f"[{_stamp()}] failure capture skipped: "
                      f"{type(exc).__name__}: {exc}")


#: Buttons this module is permitted to press, as whole-string matches.
#:
#: An allowlist rather than a denylist, because the risk is asymmetric: a
#: missing button costs a hold that David could still have got manually, and
#: an unexpected button could be the one that spends his money. Nothing that
#: completes a purchase belongs here, and nothing should be added to it
#: without a live page to check the wording against.
SAFE_BUTTONS = (
    "continue",
    "next",
    "accept and continue",
    "get tickets",
    "buy now",
    "select",
)

#: Never pressed, whatever else matches. Belt and braces around SAFE_BUTTONS:
#: if a page ever labels its payment control "Continue to payment", the
#: allowlist alone would let it through on a prefix match, so the check below
#: rejects anything containing these first.
#:
#: "find more tickets" is here for a different reason, and it is observed
#: rather than imagined: it is the button Ticketmaster puts on the dead-end
#: screen you reach when a listing has gone (see LISTING_GONE_MARKERS).
#: Pressing it throws away the listing detail page and starts the search
#: again, which would spend the rest of the 45-second window going round a
#: loop instead of reporting the truth.
#: "cancel order" is on this list for the opposite reason to the rest. It is
#: not dangerous because it spends money — it is dangerous because it throws
#: the hold away. It sits directly beside "Place Order" on the real checkout
#: page captured on 2026-08-19, which is exactly the position an automated
#: click is most likely to land on by accident.
FORBIDDEN_BUTTONS = ("pay", "place order", "confirm order", "checkout", "purchase",
                     "find more tickets", "cancel order")

#: The listing detail screen — reached by clicking a resale row.
#:
#: Observed on 2026-08-19 on a different event ("Amble", Live at the
#: Docklands), which is the same interface. This is the first direct evidence
#: that clicking a listing row leads anywhere at all, and it is why the
#: failure email can now distinguish "we never reached the listing" from "we
#: reached it and it was gone" — two failures with completely different fixes,
#: which used to produce the same message.
LISTING_DETAIL_MARKERS = ("ticket type", "section")

#: The dead end. Also observed on 2026-08-19, verbatim:
#:
#:     Sorry, these tickets are unavailable
#:     The tickets you wanted have either been sold or removed from sale.
#:
#: This is precisely the experience David described on the Electric Picnic
#: listings — the row is still on the page, and clicking it lands here. It is
#: a definite answer, not a timeout, so seeing it should stop the attempt at
#: once rather than spending the remaining seconds pressing hopefully.
LISTING_GONE_MARKERS = (
    "these tickets are unavailable",
    "sold or removed from sale",
    "tickets you wanted have either been sold",
)


#: Markers that say "this is not the event page at all".
#:
#: Written from the 12:06 and 12:07 failures of 2026-08-21, which reported
#: "no quantity stepper found" AND "the search button never became clickable",
#: before and after a reload, forty seconds apart. Two controls that are
#: always present on a real event page were both absent twice — which is not a
#: stale page, it is a different page. The likeliest candidate is a bot check,
#: and nothing in the record could confirm it because nothing wrote down where
#: the browser actually was.
INTERSTITIAL_MARKERS = (
    # Observed, verbatim, on 2026-08-22 at 11:23 UTC — the first hard
    # evidence that the BUYING browser gets challenged rather than merely
    # rate-limited. It arrived with the correct event URL, no controls, and an
    # entirely empty body, so only the title gave it away.
    "your browsing activity has been paused",
    # The dead end a refused listing lands on. Not a block, but equally not
    # the event page, and worth naming for the same reason.
    "400 - error",
    "verify you are a human", "are you a robot", "unusual activity",
    "access denied", "request blocked", "captcha", "press and hold",
    "checking your browser", "please enable javascript", "rate limit",
    "too many requests", "queue-it", "you are in line", "waiting room",
)


def challenge_markers(page) -> list:
    """Which challenge-screen markers this page matches, if any.

    Reads the TITLE as well as the body, because the screen this most needs to
    recognise has no body. Ticketmaster's block of 2026-08-22 arrived with the
    correct event URL, zero characters of readable text, and the whole story
    in the title: "Your Browsing Activity Has Been Paused".

    One reader, used by the live attempt and by the post-mortem alike. The two
    readers of the resale feed disagreed for a fortnight and neither was
    noticed, because both only ran on the losing path.
    """
    title = text = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    try:
        text = page.inner_text("body") or ""
    except Exception:
        pass
    low = " ".join(f"{text} {title}".lower().split())
    return [m for m in INTERSTITIAL_MARKERS if m in low]


def capture_failure(page, result: "HoldResult", event, attempt: int = 1) -> str:
    """Write down everything about an attempt that did not work.

    David's instruction of 2026-08-21: "capture how we failed so the next time
    we will succeed". This is that. Fourteen attempts had by then produced
    fourteen prose reasons and almost no evidence — and the one failure mode
    nobody could explain, a page with neither a quantity stepper nor a search
    button on it, is precisely the one a single URL would have settled.

    Deliberately thorough, and deliberately NOT on the critical path. The find
    recorder dropped its screenshot in August because it sat between a live
    listing and the click that might win it, where three seconds is the whole
    product. Nothing here runs until an attempt has already failed, so the
    ticket is lost either way and there is no clock left to protect. That is
    the same reasoning _probe_after_gone uses to justify its extra call.

    Never raises. A diagnostic that can break the thing it is diagnosing is
    worse than no diagnostic.
    """
    import json

    # The name must not be able to collide, and a timestamp alone cannot
    # promise that.
    #
    # This began as second resolution, which silently overwrote the earlier
    # record whenever two attempts failed in the same second — and the retries
    # now run up to seven attempts against one listing, so that is the normal
    # case rather than a rare one. Going to milliseconds made it rarer without
    # making it impossible: writing this file is fast, and two captures landed
    # inside the same millisecond on the very first test run.
    #
    # Losing the FIRST failure is the worst way to lose one. It is the attempt
    # that saw the page in the state that caused everything after it, and it
    # is the one an overwrite always takes. So the clock gets us an ordered,
    # readable name and the loop below guarantees the rest.
    config.DIAG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    base = config.DIAG_DIR / f"hold-{stamp}-{event.slug}-try{attempt}"
    spare = 2
    while base.with_suffix(".json").exists():
        base = config.DIAG_DIR / f"hold-{stamp}-{event.slug}-try{attempt}-{spare}"
        spare += 1
    record = {
        "when": _stamp(),
        "event": event.slug,
        "attempt": attempt,
        "reason": result.reason,
        "secured": result.secured,
        "timings": dict(result.timings),
        "wall_seconds": round(result.elapsed, 2),
        "still_listed_after": result.still_listed_after,
        "ever_listed_after": result.ever_listed_after,
        "ids_after": list(result.ids_after),
        "listing_id": result.listing_id,
        "landed_url": result.landed_url,
        # Decoded from landed_url rather than stored twice — see
        # read_error_context(). Recorded because "Ticketmaster said this was
        # live when it refused us" is the fact that distinguishes a lost race
        # from a listing we were never going to be allowed to take, and it was
        # sitting unread in the URL above for fifteen attempts.
        "listing_active": result.listing_active,
        "ever_active": result.ever_active,
        "offer_type": result.offer_type,
        "offer_summary": result.offer_summary,
        # The offer handles from the feed, and where the row pointed. Together
        # these answer whether a direct path exists at all — see
        # Listing.offer_ids and OfferTrace.
        "offer_ids": list(result.offer_ids),
        "used_direct": result.used_direct,
        # How many of this attempt's navigations Chrome answered by itself.
        # Anything above zero means part of what follows is not evidence.
        "cache_replays": getattr(result, "cache_replays", 0),
        "row_href": result.row_href,
        "trace": list(result.trace),
        "notes": list(result.notes),
    }
    text = ""
    try:
        record["url"] = page.url
    except Exception:
        record["url"] = ""
    try:
        record["title"] = page.title()
    except Exception:
        record["title"] = ""
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""

    # Which of the controls a real event page always has were actually there.
    # This is the line that would have answered 12:06 in one glance.
    #
    # Matched against the TITLE as well as the body, because the one page this
    # most needs to recognise has no body at all. Ticketmaster's challenge
    # screen of 2026-08-22 arrived with the right URL, zero characters of
    # readable text and the title "Your Browsing Activity Has Been Paused" —
    # so a body-only matcher, which is what this was, found nothing to report
    # about the most important page it had ever been shown.
    low = " ".join(f"{text or ''} {record.get('title') or ''}".lower().split())
    record["page"] = {
        "text_chars": len(text or ""),
        "has_find_tickets": "find tickets" in low,
        "has_search_again": "search again" in low,
        "has_resale_panel": "verified resale" in low,
        "looks_like_interstitial": [m for m in INTERSTITIAL_MARKERS if m in low],
    }
    try:
        record["page"]["has_quantity_stepper"] = (
            page.get_by_role("spinbutton").first.is_visible(timeout=2_000))
    except Exception:
        record["page"]["has_quantity_stepper"] = False
    # Truncated, because a Ticketmaster page is enormous and the first part is
    # where the headline of a block page lives.
    record["text_excerpt"] = (text or "")[:4000]

    try:
        config.DIAG_DIR.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".json").write_text(json.dumps(record, indent=2))
    except Exception as exc:
        print(f"[{_stamp()}] could not write the failure record: {exc}")
        return ""

    # And a picture, which is the one thing that settles "what page IS this".
    # Bounded and optional: a failed screenshot must not turn a recorded
    # failure into an unrecorded one.
    if config.HOLD_SCREENSHOTS:
        try:
            page.screenshot(path=str(base.with_suffix(".png")),
                            full_page=False, timeout=5_000)
        except Exception:
            pass

    print(f"[{_stamp()}] how it failed is recorded: {base.with_suffix('.json')}")
    if record["page"]["looks_like_interstitial"]:
        print(f"[{_stamp()}] NOT THE EVENT PAGE — this looks like a block or "
              f"challenge screen: "
              f"{', '.join(record['page']['looks_like_interstitial'])}")
    return str(base.with_suffix(".json"))


def _probe_after_gone(session, event, listing, result: "HoldResult", page) -> None:
    """Ask the resale feed whether the ticket Ticketmaster just refused is gone.

    Runs the instant the dead-end screen is recognised, and never raises: this
    is diagnosis, and a failed diagnosis must not change a failure into an
    exception on the one path where David is already not getting a ticket.

    Costs one same-origin XHR from a page that is already open, which is the
    same call the sweep makes every ninety seconds. Worth it: this is the only
    moment the question can be asked, and the answer decides what the whole
    project should do next. If the ticket is still in the feed after being
    refused, then chasing seconds is chasing nothing — the listing was never
    takeable in that moment, and the winning move is to come back when the
    other basket lapses.

    Called from both failure paths that can ask it: the dead-end screen after
    a click, and the panel that never drew the row. They are different
    failures with the same question behind them, and they had two different
    readers of one endpoint until 2026-08-21 — one of which quietly disagreed
    with this one about which key to read, and kept its answer in a local.

    Also captures the URL the attempt reached, which is the only place a
    direct link to a single listing has ever been observed. If that URL turns
    out to carry the listing id, a later attempt can navigate straight to it
    and skip the navigate-quantity-search-panel sequence that costs most of
    the attempt.
    """
    try:
        result.landed_url = page.url or ""
        if result.landed_url:
            result.note(f"stopped at: {result.landed_url}")
    except Exception:
        pass

    # Read the refusal itself before asking anyone else about it.
    #
    # Ordered first deliberately. The feed call below is the expensive,
    # failure-prone half of this function — it needs a second tab because the
    # dead end is on the wrong origin — while this is a string parse of a URL
    # already in hand. It also answers a better question: the feed can only
    # say whether the listing is offerable to us this second, and a ticket in
    # somebody's basket is not, which is why a vanished listing has been
    # recorded as sold fifteen times when Ticketmaster's own payload said it
    # was still active.
    context = read_error_context(result.landed_url)
    offer = context.get("listing") if isinstance(context, dict) else None
    if isinstance(offer, dict):
        active = offer.get("active")
        result.listing_active = bool(active) if isinstance(active, bool) else None
        result.offer_type = str(offer.get("offerType") or "")
        result.offer_summary = describe_offer(offer)
        # Prefer Ticketmaster's own id for the listing over ours. `urlId` is
        # the same short string the feed calls resaleListingId, and having it
        # from the refusal proves which listing was refused — the ids have
        # been observed to change between polls for what is plainly the same
        # ticket, so the one in the error payload is the one to trust.
        url_id = str(offer.get("urlId") or "")
        if url_id:
            result.listing_id = url_id
        if result.listing_active:
            result.note(
                f"Ticketmaster's own error page says this listing is STILL "
                f"ACTIVE — it has not sold. {result.offer_summary or 'no detail'}"
            )
        elif result.listing_active is False:
            result.note("Ticketmaster's error page says the listing is no "
                        "longer active — this one really did go")

    try:
        # Ask from where we are standing first: on the no-row path that is the
        # event page itself, and the answer costs nothing.
        record = session.listings_now(event, config.WANTED_QUANTITY)
        data = (record or {}).get("data")
        if not isinstance(data, dict):
            # The dead-end path always lands here, because the screen is on a
            # different host than the endpoint. Open a tab that can ask.
            asker = getattr(session, "listings_from_origin", None)
            if asker is not None:
                result.note("this screen cannot reach the resale endpoint — "
                            "asking from a tab on the event page instead")
                record = asker(event, config.WANTED_QUANTITY)
                data = (record or {}).get("data")
        if not isinstance(data, dict):
            result.note("could not ask the resale feed whether it really sold")
            return
        # Both fields are checked for type before being believed. A `picks`
        # that is not a list still has a len() — a string of ten characters
        # reports ten listings — and that would answer "still listed" from
        # nothing at all, which is the one wrong answer with a cost attached:
        # it tells David to keep going back to a ticket that really has sold.
        picks = data.get("picks")
        picks = picks if isinstance(picks, list) else None
        total = data.get("total")
        total = total if isinstance(total, int) else None
        if picks is None and total is None:
            result.note(f"the resale feed answered in a shape this does not "
                        f"know how to read: keys={sorted(data)}")
            return
        result.ids_after = [str(p.get("resaleListingId") or p.get("id"))
                            for p in (picks or []) if isinstance(p, dict)
                            and (p.get("resaleListingId") or p.get("id"))]
        result.still_listed_after = bool(
            total if total is not None else len(picks))
        wanted = getattr(listing, "listing_id", "") or ""
        result.listing_id = wanted
        if result.still_listed_after:
            same = wanted and wanted in result.ids_after
            result.note(
                f"the feed still lists {len(result.ids_after) or total} ticket(s) "
                f"right after the refusal — id(s) {', '.join(result.ids_after) or '?'}"
                + (" (the same one we tried)" if same else
                   f" (we tried {wanted or '?'}, which is NOT among them)"
                   if wanted else "")
            )
        else:
            result.note("the feed agrees: nothing left. It really did go.")
    except Exception as exc:
        # Left as None deliberately: an exception here means the question was
        # never answered, and the callers branch on True / False / None to
        # tell "it sold" from "we could not tell".
        result.note(f"could not ask the resale feed: {type(exc).__name__}")


#: Where a button's label can hide. Read in this order, most human-meaningful
#: first, and every one of them is vetted before anything is pressed.
_LABEL_ATTRIBUTES = ("aria-label", "title", "value")


def button_labels(button) -> list:
    """Every string that might be this button's label, lowercased, no blanks.

    This exists because Playwright matches `get_by_role(name=...)` against the
    ACCESSIBLE name, and the accessible name is not always the rendered text.
    A control labelled only by `aria-label` — or by `title`, or by the `value`
    of an `<input type="submit">` — has an accessible name and no inner text
    at all.

    The guard below used to vet `inner_text()` alone. Such a button therefore
    reached is_forbidden() as the empty string, which is forbidden by nothing,
    and would have been clicked. That is not a hypothetical: it is precisely
    the hole FORBIDDEN_BUTTONS was written to close. A control whose
    accessible name is "Continue to payment" matches the allowlist entry
    "continue", renders no text of its own, and sailed through the one check
    standing between this module and David's card.

    So collect every candidate and check them all. Each source is read
    separately, because one of them raising must not cost us the others — an
    aria-label we can read is worth more than an inner_text we cannot.
    """
    labels = []

    def add(value) -> None:
        text = (value or "").strip().lower()
        if text and text not in labels:
            labels.append(text)

    try:
        add(button.inner_text(timeout=1_500))
    except Exception:
        pass
    for attribute in _LABEL_ATTRIBUTES:
        try:
            add(button.get_attribute(attribute, timeout=1_500))
        except Exception:
            pass
    return labels


#: The exact accessible name of the control that reserves a resale ticket.
#:
#: Observed on the real checkout of 2026-08-26 00:53, not guessed. That page
#: has exactly one forward control, and Ticketmaster's own warning beside it
#: reads "Proceed to payment to reserve these tickets" — so this button IS the
#: reservation step. There is no separate hold, basket or reserve control to
#: press instead; the stepper goes 1 Your Order, 2 Payment, 3 Confirmation,
#: and this moves 1 to 2.
RESERVE_BUTTON = "continue to payment"

#: Anything that could complete a purchase, checked on the page AFTER the
#: reserve step. Nothing here is ever pressed by this module under any
#: circumstance or configuration.
NEVER_PRESS = ("pay now", "place order", "confirm", "complete purchase",
               "submit payment", "buy")


def _reserve_at_checkout(page, result: HoldResult) -> bool:
    """Press the one control that reserves the ticket, and stop dead after it.

    True when the reservation step was taken.

    ── Why this exists, and why it is allowed to press a "pay" button ───────

    The watcher reached a live checkout at 00:53 on 2026-08-26 and could go no
    further, because FORBIDDEN_BUTTONS blocks anything containing "pay" and
    the only forward control on that page is labelled "Continue To Payment".
    The blanket rule was right while nobody knew what that button did. The
    captured page settles it: beside it Ticketmaster writes "Proceed to
    payment to reserve these tickets", so pressing it is what turns a page
    anyone can still take from you into a ticket that is yours to pay for.

    David chose this on 2026-08-26 knowing the trade — it is the same scope he
    set on 2026-08-19, secure it and hand off for payment, and this is the
    step that secures.

    ── What keeps it safe ──────────────────────────────────────────────────

    Three things, and they are independent of each other:

      * It presses ONE button, matched on its exact accessible name, and only
        when that name is exactly RESERVE_BUTTON. Not a prefix, not a
        substring, not the first thing that looks like a Continue.
      * After pressing, it returns. It never presses anything again, on any
        page, for the rest of the attempt. There is no loop after this point.
      * NEVER_PRESS is checked against every label on the button first, so a
        page that relabels its final purchase control "Continue to payment"
        cannot slip through the exact match either.

    Card details are never entered, and the screen this lands on is where the
    watcher stops for good.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    if not config.RESERVE_AT_CHECKOUT:
        result.note("not pressing 'Continue To Payment' — reserving is "
                    "switched off (EP_RESERVE_AT_CHECKOUT=0)")
        return False
    try:
        # The policy tick is a precondition, not a nicety: the button does not
        # advance without it. Failing to find it is not fatal — some accounts
        # may have it pre-agreed — so this tries and carries on either way.
        for label in ("I agree to the Ticket Exchange Policy",
                      "Ticket Exchange Policy"):
            try:
                box = page.get_by_role("checkbox", name=label, exact=False).first
                if box.is_visible(timeout=1_500) and not box.is_checked():
                    box.check(timeout=3_000)
                    result.note("ticked the Ticket Exchange Policy box — "
                                "required before the page will advance")
                    break
            except (PlaywrightTimeout, PlaywrightError):
                continue

        button = page.get_by_role("button", name=RESERVE_BUTTON, exact=True).first
        if not button.is_visible(timeout=3_000):
            result.note("no 'Continue To Payment' control on this page")
            return False
        labels = button_labels(button)
        if not labels:
            result.note("refusing to press an unlabelled reserve control")
            return False
        bad = next((l for l in labels
                    if any(n in l.lower() for n in NEVER_PRESS)), None)
        if bad is not None:
            result.note(f"refusing to press {bad!r} — that completes a purchase")
            return False
        button.click(timeout=8_000)
        result.note("pressed 'Continue To Payment' — this is the step that "
                    "reserves the ticket. STOPPING HERE: no card details are "
                    "entered and nothing further will be pressed.")
        result.reserved = True
        return True
    except (PlaywrightTimeout, PlaywrightError) as exc:
        result.note(f"could not take the reserve step ({type(exc).__name__}) — "
                    f"the checkout page is still open for David")
        return False


def _press_one_safe_button(page, result: HoldResult) -> bool:
    """Press the first permitted button visible. True if one was pressed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for name in SAFE_BUTTONS:
        try:
            button = page.get_by_role("button", name=name, exact=False).first
            if not button.is_visible(timeout=1_500):
                continue
            labels = button_labels(button)
            # Nothing readable at all, from any source. Refuse.
            #
            # An unidentifiable button is not a safe one, and the two mistakes
            # do not cost the same: skipping a real "Continue" loses a hold
            # David could still have made by hand, while pressing a control
            # nobody could read is how this module spends his money. The
            # allowlist matched the accessible name, so SOMETHING labels this
            # button — if we cannot see what, that is a reason to stop.
            if not labels:
                result.note(
                    f"refusing to press an unlabelled button matching {name!r} "
                    f"— nothing readable to check it against"
                )
                continue
            forbidden = next((l for l in labels if is_forbidden(l)), None)
            if forbidden is not None:
                result.note(
                    f"refusing to press {forbidden!r} — that is a payment control"
                )
                continue
            button.click(timeout=5_000)
            result.note(f"pressed {labels[0]!r}")
            return True
        except (PlaywrightTimeout, PlaywrightError):
            continue
    return False


def is_forbidden(label: str) -> bool:
    """Would pressing this button risk completing a purchase?

    Substring, deliberately. "Continue to payment" must be caught by "pay",
    and it would not be by a whole-word rule.
    """
    lowered = (label or "").strip().lower()
    return any(bad in lowered for bad in FORBIDDEN_BUTTONS)


#: The countdown Ticketmaster puts on a live checkout, e.g. "11:39".
#:
#: Read rather than assumed. The hold length is the one number David has to
#: act on and it is not published; the 11:39 observed on 2026-08-19 came from
#: a different event, and there is no reason a festival resale listing must
#: get the same window as a boxing match at Croke Park. So the alert says the
#: real number when it can see one, and falls back to the configured estimate
#: only when it cannot.
COUNTDOWN_RE = __import__("re").compile(r"\b([0-9]{1,2}):([0-5][0-9])\b")



#: Longest hold worth believing. The one observed was 11:39; a match above
#: this is far more likely to be an event time than a countdown.
COUNTDOWN_MAX_MINUTES = 20


def read_countdown_minutes(page) -> Optional[float]:
    """Minutes left on the checkout clock, read off the page. None if absent.

    Three rules, each earning its place, and all three are needed because a
    checkout page is full of times that are not the hold — the event's own
    start time most of all.

    1. WHOLE LINE ONLY. A countdown stands alone on its line; every other time
       on a checkout is embedded in a sentence — "Sat, 5 Sept 2026, 16:00",
       "Doors 19:00". Matching anywhere in the text reports the event's start
       time as the time remaining, because 16:00 parses as a perfectly
       plausible sixteen-minute hold. On the page captured on 2026-08-19 the
       countdown was alone on its own line, printed twice, above the word
       "Checkout".

    2. NOT ON THE MINUTE. A time written mm:00 is almost always a clock rather
       than a countdown: an event time is written 16:00, while a countdown
       shows :00 for one second in sixty. Skipping those costs a measurement
       once in every sixty holds and avoids reporting an event time as the
       time remaining.

    3. FIRST DOWN THE PAGE, not smallest. The captured page put the countdown
       at the very top, and it is the page's own most prominent clock. This
       used to take the smallest match anywhere instead, which worked on the
       real page purely by luck — 11:39 happened to be smaller than everything
       else on it — and would have preferred a stray "02:15" further down over
       the genuine countdown above it.

    Every rule fails towards None, and None means the alert uses
    config.HOLD_MINUTES_HINT and says it is an estimate. Getting this wrong in
    the cautious direction costs the measurement; getting it wrong the other
    way tells David he has sixteen minutes when he has two.

    One hole is left open knowingly: a bare "19:30" alone on a line would
    still read as a 19½-minute hold. Nothing observed does that, and the only
    fix would be to require the countdown to sit near a word like "left",
    which no captured page reliably carries.
    """
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return None

    for line in text.splitlines():
        match = COUNTDOWN_RE.fullmatch(line.strip())
        if not match:
            continue
        mins, secs = int(match.group(1)), int(match.group(2))
        if secs == 0:
            continue                      # a clock, not a countdown — see rule 2
        minutes = mins + secs / 60.0
        if 0 < minutes <= COUNTDOWN_MAX_MINUTES:
            return minutes                # first one down the page — see rule 3
    return None


def describe_lapse(days_left) -> str:
    """"in about 3 hours", "in 12 days", "already" — never "in 0 day(s)".

    The old wording printed `days_left` rounded to one decimal and appended
    "day(s)", so anything under about ninety minutes rendered as "0 day(s)" —
    which is either alarming or meaningless depending on how closely it is
    read, and is exactly the line that decides whether David goes and signs in
    again before a listing appears. Seen for real in `doctor` on 2026-08-19.

    Deliberately vague above a day and precise below one, because that is
    where the accuracy is worth anything: "12 days" needs no action today,
    "about 2 hours" does.
    """
    if days_left is None:
        return "at an unknown time"
    if days_left <= 0:
        return "already"
    hours = days_left * 24.0
    if hours * 60 < 1:
        # Positive but under a minute. "in about 0 minutes" is the shape of
        # bug this function exists to prevent, so say the true thing instead.
        return "within the minute"
    if hours < 1.5:
        return f"in about {max(1, int(round(hours * 60))) } minutes"
    if days_left < 1:
        return f"in about {hours:.0f} hours"
    if days_left < 2:
        return "in about a day"
    return f"in about {days_left:.0f} days"


def _page_says(page, markers, all_of: bool = False) -> bool:
    """Is this page showing these words? Never raises.

    `all_of` distinguishes the two kinds of question asked of it. A dead end
    is recognised by any one of several phrasings, while the listing detail
    screen is recognised by several labels appearing TOGETHER — "section"
    alone appears on the search results too, so any-of would call every page
    the detail page.
    """
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    hits = (marker in text for marker in markers)
    return all(hits) if all_of else any(hits)


def _basket_is_live(page, markers) -> bool:
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in markers)


def _find_listing_row(page, listing, result: HoldResult):
    """The clickable row for this listing, or None.

    Prefers the section, which is the one field both the API and the rendered
    page agree on. Falls back to any resale row, because one listing is the
    overwhelmingly common case — of the nine sightings up to 2026-08-18,
    every one was a single listing.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    section = getattr(listing, "section", None)
    if section:
        try:
            row = page.get_by_text(f"Section {section}", exact=False).first
            if row.is_visible(timeout=5_000):
                result.note(f"found the row for Section {section}")
                return row
        except (PlaywrightTimeout, PlaywrightError):
            result.note(f"no row matched Section {section} — trying any resale row")

    try:
        row = page.get_by_text("Verified Resale Ticket", exact=True).first
        if row.is_visible(timeout=5_000):
            result.note("found a Verified Resale row")
            return row
    except (PlaywrightTimeout, PlaywrightError):
        pass
    return None


class _NoteSink:
    """Adapter so browser.py's Reading-shaped helpers can write into a HoldResult.

    `_set_quantity` takes something with .note(); a HoldResult has one, but
    going through an adapter keeps the two types from growing into each other.
    """

    def __init__(self, result: HoldResult):
        self._result = result

    def note(self, text: str) -> None:
        self._result.note(text)
