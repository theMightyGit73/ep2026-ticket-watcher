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

import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import config
from .state import stamp


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

        if not session.signed_in():
            result.reason = (
                "the buying browser is not signed in — run "
                "`python -m ep_watcher login-buy`"
            )
            result.note(result.reason)
            return result

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
