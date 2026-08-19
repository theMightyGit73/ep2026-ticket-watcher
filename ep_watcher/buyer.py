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

import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Optional

from . import config
from .state import stamp

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


def record_signed_in_fingerprint(profile_dir=None) -> dict:
    """Remember what this profile looked like at the moment of signing in.

    Called by `login-buy` once David confirms he is signed in. The cookies
    that are present now but were not on a signed-out profile are, by
    construction, the ones the account is carried in. Nobody has to guess
    their names.
    """
    cookies = profile_cookies(profile_dir)
    auth = sorted(set(cookies) - KNOWN_ANONYMOUS_COOKIES)
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "auth_cookies": auth,
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
        # A profile that predates the fingerprint, or one whose record was
        # lost. Fall back to the shipped anonymous set, and say that the
        # answer is weaker than it would otherwise be.
        extra = sorted(set(cookies) - KNOWN_ANONYMOUS_COOKIES)
        if extra:
            out.update(
                signed_in=True,
                reason=f"{len(extra)} cookie(s) beyond the anonymous set "
                       f"(no sign-in fingerprint recorded — re-run login-buy "
                       f"to make this check exact)",
            )
        else:
            out.update(signed_in=False,
                       reason="only anonymous cookies present — not signed in")
        return out

    expected = set(recorded.get("auth_cookies") or [])
    if not expected:
        out.update(reason="the recorded sign-in found no account cookies to watch")
        return out

    missing = sorted(expected - set(cookies))
    if missing:
        out.update(
            signed_in=False,
            reason=f"the session cookie(s) recorded at sign-in are gone "
                   f"({', '.join(missing[:3])}) — it has been signed out",
        )
        return out

    # Still present. How long have they got?
    expiries = [cookies[n] for n in expected if cookies.get(n)]
    soonest = min(expiries) if expiries else None
    out.update(signed_in=True, reason="the cookies recorded at sign-in are all present")
    if soonest:
        left = (soonest - datetime.now(timezone.utc)).total_seconds() / 86400.0
        out.update(expires_at=soonest.isoformat(), days_left=round(left, 1))
        if left <= 0:
            out.update(signed_in=False,
                       reason="the session cookies have expired — sign in again")
    return out


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
    #: Roughly how long he has, for wording only. Ticketmaster does not
    #: publish the number and we do not scrape it.
    minutes_hint: int = 0

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"    [buyer] {text}")


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

    def set_quantity(self, qty: int, result: "HoldResult") -> None:
        """Drive the page's quantity stepper, reusing the watcher's logic.

        The stepper is a role=spinbutton driven with arrow keys because the
        page floats an overlay over it that eats real clicks — knowledge that
        cost a day to find and must not be duplicated here and left to drift.
        """
        self._session._set_quantity(qty, _NoteSink(result))

    def signed_in(self) -> bool:
        """Is this profile actually logged in?

        Checked before clicking anything, because the failure mode otherwise
        is silent: a signed-out session can reach a listing and then bounce to
        a login wall with the ticket still unheld, which reads from the logs
        as 'we tried and it was gone'.
        """
        text = self._session.visible_text().lower()
        return "sign out" in text or "my account" in text


def secure(session: BuySession, event, listing, result: HoldResult = None) -> HoldResult:
    """Put `listing` in a basket. Returns without paying, always.

    `session` must already be started and signed in. Failure at any step is
    recorded and returned rather than raised: the caller's next move is to
    send the ordinary "a ticket is live" alert, which must not be lost
    because this optimistic extra step went wrong.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    from .sources.browser import BASKET_MARKERS, SEARCH_BUTTONS, _is_listing_row

    result = result or HoldResult()
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

    try:
        # Navigate BEFORE asking whether we are signed in. A freshly started
        # BrowserSession is parked on about:blank, which contains neither
        # "sign out" nor "my account" — so checking first meant the answer was
        # always "not signed in", and the whole feature was a no-op that would
        # have reported a login problem on the first real listing. Caught by
        # reading the flow back on 2026-08-19, before any listing tested it.
        page = session.page
        page.goto(event.url, wait_until="domcontentloaded")
        result.note(f"opened {event.slug} in the buying browser")

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

        # Same quantity discipline as the watcher: the page defaults to 2 and
        # resale results are filtered by quantity, so asking for the wrong
        # number manufactures a refusal against a listing that is really there.
        session.set_quantity(config.WANTED_QUANTITY, result)
        try:
            page.get_by_role("button", name=SEARCH_BUTTONS).first.click(timeout=15_000)
            result.note(f"searched for {config.WANTED_QUANTITY}")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            result.reason = f"could not press search in the buying browser: {exc}"
            result.note(result.reason)
            return result

        if out_of_time():
            return result

        # Find the listing row. Matched on the section rather than on the
        # listing id, because the id is an API field and has never been seen
        # in the rendered page — and section plus price is what distinguishes
        # one row from another when several are live.
        row = _find_listing_row(page, listing, result)
        if row is None:
            result.reason = (
                "the listing was gone from the page by the time the buying "
                "browser reached it"
            )
            result.note(result.reason)
            return result

        try:
            row.click(timeout=10_000)
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
        for _ in range(4):
            if out_of_time():
                return result
            if _basket_is_live(page, BASKET_MARKERS):
                break
            if not _press_one_safe_button(page, result):
                break
            time.sleep(1.5)

        if _basket_is_live(page, BASKET_MARKERS):
            result.secured = True
            result.minutes_hint = config.HOLD_MINUTES_HINT
            result.note("BASKET CONFIRMED — the ticket is held; stopping here")
            # Bring it to the front so the machine he walks to is already
            # showing the thing he has to finish.
            try:
                page.bring_to_front()
            except Exception:
                pass
            return result

        result.reason = (
            "clicked through but no basket appeared — the listing was probably "
            "taken while we were in it"
        )
        result.note(result.reason)
        return result

    except Exception as exc:
        # Never let this cost the ordinary alert. Whatever happened, David
        # still needs to be told a ticket existed.
        result.reason = f"{type(exc).__name__}: {exc}"
        result.note(f"secure attempt failed — {result.reason}")
        return result


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
FORBIDDEN_BUTTONS = ("pay", "place order", "confirm order", "checkout", "purchase")


def _press_one_safe_button(page, result: HoldResult) -> bool:
    """Press the first permitted button visible. True if one was pressed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for name in SAFE_BUTTONS:
        try:
            button = page.get_by_role("button", name=name, exact=False).first
            if not button.is_visible(timeout=1_500):
                continue
            label = (button.inner_text(timeout=1_500) or "").strip().lower()
            if is_forbidden(label):
                result.note(f"refusing to press {label!r} — that is a payment control")
                continue
            button.click(timeout=5_000)
            result.note(f"pressed {label!r}")
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
