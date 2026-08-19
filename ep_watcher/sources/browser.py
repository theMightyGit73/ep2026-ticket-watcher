"""Source: a real, visible Chrome driven by Playwright.

Everything in this module was verified against the live page on 2026-08-13,
because three reasonable-sounding assumptions about it turned out to be wrong.
Worth writing down, since each one silently produces a watcher that reports
"no tickets" forever:

  1. Headless does not work. Headless Chrome gets HTTP 403 on every attempt.
     The same profile, headed, gets 200. This is not tunable with flags or
     user-agent strings — it must be a real, visible browser. That is why
     config.HEADLESS defaults to False and why the watcher runs on David's
     Mac rather than in CI: a datacentre IP fails for the same reason.

  2. The first request is *supposed* to fail. ticketmaster.ie answers the
     first load with 401 and a bot-check page whose real content sits inside
     <noscript> — so it looks blank rather than obviously walled. Accept the
     cookie dialog, reload, and the second request returns 200 and the real
     page. cloudscraper never got past this step, which is the entire reason
     the old watcher logged 657 consecutive failures.

  3. The resale section does not exist until you search. On a fresh load the
     page ends at the "Find Tickets" button. "Other Options → Verified Resale
     Tickets" is rendered by the search response, not by the page. So there is
     no read-only way to watch resale: pressing the button is not the risky
     optional extra, it is the only thing that produces any signal at all.

  And one trap: the sentence "Resale Tickets will appear below when they are
  available." is a STATIC caption. It is present even while a real resale
  listing is displayed directly beneath it. Treating it as an empty-state
  marker — which is the obvious reading — inverts the result.
"""

import re
import time
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .. import config
from ..model import AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading, better_status as _better
from ..state import stamp

SOURCE = "browser"

# ── Text anchors, all confirmed against the live page ────────────────────────
CONSENT_BUTTONS = ("Accept Cookies", "Accept All", "Accept")
SEARCH_BUTTONS = re.compile(r"^(find tickets|search again|search for tickets)$", re.I)

REAL_PAGE_MARKERS = ("find tickets", "search again", "electric picnic")
NOT_ENOUGH = "there arent enough tickets to complete your request"
#: The "Other Options → Verified Resale Tickets" panel heading. Its presence
#: means the resale section rendered; it says nothing about whether there is
#: anything in it.
RESALE_HEADING = "verified resale"
#: One actual listing. This is the signal — NOT the absence of the panel's
#: static "will appear below when they are available" caption, which sits
#: there permanently, including directly above a real listing.
RESALE_MARKER = "verified resale ticket"
#: A live basket, recognised by what the real checkout page actually says.
#:
#: The first three were written from memory of how such pages usually look and
#: are kept only in case an older flow still uses them. On 2026-08-19 David
#: sent back the genuine Ticketmaster checkout page for a held ticket, and NOT
#: ONE of them appeared on it. A successful hold would therefore have been
#: reported as a failure — the single worst outcome this module can produce,
#: because the ticket really is held, the clock really is running, and the one
#: person who could finish it has just been told it did not work.
#:
#: What the real page carries is "Place Order" and "Cancel Order", both as
#: controls and in the terms sentence beneath them. Those are specific to a
#: checkout with something in it.
#:
#: The asymmetry decides the design. Claiming a hold that is not there costs a
#: walk to the laptop and an obvious empty screen. Missing one that IS there
#: costs the ticket, silently, while it expires. So this errs towards
#: recognising.
BASKET_MARKERS = (
    "place order",
    "cancel order",
    "time left to complete",
    "your tickets are reserved",
    "proceed to checkout",
)
SOLD_OUT_HINTS = ("tickets are currently unavailable", "this event is sold out")

#: The call that populates "Other Options → Verified Resale Tickets".
#: Captured from the live page: GET /api/quickpicks/{eventId}/resale?qty=...
RESALE_API_RE = re.compile(r"/api/quickpicks/[^/]+/resale", re.I)

#: Ticketmaster event pages end in /event/{ID}. The id is what the resale
#: endpoint is keyed on, so it can always be recovered from the URL of the
#: page being watched — no configuration needed.
EVENT_ID_RE = re.compile(r"/event/([A-Za-z0-9]+)", re.I)


def _event_id_from_url(url: str) -> str:
    m = EVENT_ID_RE.search(url or "")
    return m.group(1) if m else ""


PRICE_RE = re.compile(r"€\s?(\d{1,4}[.,]\d{2})")
SECTION_RE = re.compile(r"^section\s+(.+)$", re.I)


def _normalise(text: str) -> str:
    """Lowercase and drop apostrophes so curly-vs-straight can't break a match."""
    return re.sub(r"[’'`]", "", text or "").lower()


def _is_listing_row(line: str) -> bool:
    """Is this line one actual resale listing?

    Whole-line equality, never a substring test, and that is not fussiness.
    The panel's heading is "Verified Resale Tickets" and it *contains*
    "Verified Resale Ticket" — so `RESALE_MARKER in text` is true for a panel
    that is rendered and completely empty. Anything asking "are there
    listings" has to use this one rule, or the answers drift apart.
    """
    return line.strip().lower() == RESALE_MARKER


def _has_listing_rows(text: str) -> bool:
    return any(_is_listing_row(line) for line in (text or "").splitlines())


def _listing_from_pick(pick) -> Listing:
    """Best-effort description of one listing from the resale API.

    Never fails. An unrecognised entry still becomes a listing, because the
    alert's job is to say a ticket exists — describing its section wrongly
    costs a line of text, missing it costs the ticket.

    The real shape WAS observed, at last, on 2026-08-18 at 10:35 UTC — the
    first find captured since the watcher started writing them down:

        {"id": "l27t4h2d", "type": "general-seating", "section": "STNDN1",
         "originalPrice": 366.39, "description": "WEEKEND CAMPING",
         "areaName": "GA", "offerIds": ["HF6GYMRXOQ2GQMTE"],
         "resaleListingId": "l27t4h2d", "sellerBusinessType": "private", ...}

    The price key is `originalPrice`, which was not among the five names this
    guessed at — so the alert for that listing went out with a section and no
    price, which is the one detail that says whether a ticket is worth having.
    Cross-checked against the find of 2026-08-17, whose price was read off the
    rendered page as €366.39 for an equivalent listing in STNDN2: the same
    number the API reports here. Treat it as the price the page displays;
    Ticketmaster may still add fees at checkout.

    The guesses are kept behind it. This is one observation of one listing,
    and a general-admission festival ticket is the simplest case there is —
    a seated event may well carry rows and a different price field.
    """
    if not isinstance(pick, dict):
        return Listing(name=f"Verified Resale ({pick})", kind="resale")

    def first(*names):
        for name in names:
            value = pick.get(name)
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or value.get("label")
            if value not in (None, "", []):
                return value
        return None

    section = first("section", "sectionName", "area", "areaName", "zone")
    row = first("row", "rowName")
    desc = first("description", "descriptionName", "ticketType", "name", "label")
    # `originalPrice` first, because that is the key a real listing actually
    # used. The rest stay as fallbacks for shapes not yet seen.
    price = first("originalPrice", "price", "faceValue", "amount", "total",
                  "displayPrice")

    bits = ["Verified Resale"]
    if section:
        bits.append(f"— Section {section}")
    if row:
        bits.append(f"Row {row}")
    if desc and str(desc) != str(section):
        bits.append(f"({desc})")

    if isinstance(price, (int, float)):
        price = f"€{float(price):.2f}"
    elif price is not None:
        price = str(price)

    # `resaleListingId` first, then `id` — the one observed listing carried
    # both with the same value, but the API's own naming says which of the two
    # is meant to be the listing's identity.
    listing_id = first("resaleListingId", "id")

    return Listing(
        name=" ".join(str(b) for b in bits),
        price=price,
        kind="resale",
        listing_id=str(listing_id) if listing_id is not None else None,
        section=str(section) if section is not None else None,
    )


def _parse_resale_json(record, reading: Reading) -> bool:
    """Read listings from the resale API response. True if it answered.

    The page fetches its listings as JSON and then draws them. Reading the
    JSON is reading the fact; reading the page is reading its echo, and the
    echo arrives late — waiting for it could not tell "not drawn yet" from
    "drawn and empty", which recorded about a quarter of polls as
    resale-blind.

    Shape captured live on 2026-08-18 with an empty panel:

        {"quantity": 0, "total": 0, "picks": [], "descriptions": []}

    `total` is unambiguous in a way the rendered page never was: zero really
    is zero, with no question of whether anything finished rendering.

    A plain function taking the captured record, not a method: it needs no
    browser, and requiring a live session to test a data transform is how
    helpers end up untested.
    """
    if not record or not isinstance(record.get("data"), dict):
        return False

    status = record.get("status")
    if status == 403:
        # A refusal on the resale endpoint alone, while the page itself still
        # serves. Nothing used to notice: resale fell through to UNKNOWN,
        # primary still answered definitively, and the poll was filed as a
        # clean success — so no block was recorded, no profile was reset and
        # the failure counter stayed at zero. Seen on 2026-08-17 at 22:18.
        #
        # Marked blocked so the same machinery that handles a walled page
        # load handles this: reset the identity, count the episode, and let a
        # persistent version of it escalate to the watchdog. A watcher that
        # cannot see resale is not healthy, whatever primary says.
        reading.blocked = True
        reading.note(
            "resale API returned HTTP 403 — refused on the resale endpoint while "
            "the page still loads. Counted as a block, not as a quiet UNKNOWN."
        )
        return True
    if status != 200:
        reading.note(f"resale API returned HTTP {status} — falling back to the page")
        return False

    data = record["data"]
    picks = data.get("picks")
    total = data.get("total")
    if picks is None and total is None:
        reading.note(f"resale API shape unrecognised: keys={sorted(data)} — falling back")
        return False

    picks = picks if isinstance(picks, list) else []
    count = total if isinstance(total, int) else len(picks)

    if count <= 0:
        reading.resale = UNAVAILABLE
        reading.note("resale API: no listings (total=0) — definitive, nothing rendered")
        return True

    reading.resale = AVAILABLE
    reading.note(f"resale API: {count} listing(s)")
    if picks and isinstance(picks[0], dict):
        # Logged so a listing in a shape we have not seen teaches us its schema
        # rather than being described badly and forgotten. The keys of the one
        # observed listing are recorded in _listing_from_pick.
        reading.note(f"resale pick keys: {sorted(picks[0])}")
        # The seller's own id for the listing. Kept in the log rather than in
        # the listing description, because the description drives the
        # new-listing diff — and an id that turned out to be regenerated per
        # poll would make the same ticket look new every time and re-alert on
        # a four-minute clock. One sighting is not enough to know that.
        ids = [p.get("resaleListingId") or p.get("id") for p in picks
               if isinstance(p, dict) and (p.get("resaleListingId") or p.get("id"))]
        if ids:
            reading.note(f"resale listing id(s): {', '.join(str(i) for i in ids)}")

    for pick in picks[:10]:
        reading.listings.append(_listing_from_pick(pick))
    if not picks:
        reading.listings.append(
            Listing(name="Verified Resale (count only, no detail returned)", kind="resale")
        )
    return True


class BrowserSession:
    """A warm Chrome, intended to be held open across many polls."""

    def __init__(self, headless: Optional[bool] = None, profile_dir=None):
        """`profile_dir` overrides the shared profile.

        Chrome takes an exclusive lock on a user-data-dir, so a manual
        `check` while the service is running fails with "profile already in
        use" — a diagnostic that only works when the thing being diagnosed is
        stopped is not much of a diagnostic. Passing a scratch directory lets
        the two coexist. The cost is that a fresh profile has to clear the
        bot check from cold, which it does.
        """
        self.headless = config.HEADLESS if headless is None else headless
        self.profile_dir = Path(profile_dir) if profile_dir else config.PROFILE_DIR
        self._pw = None
        self._ctx = None
        self._page = None
        # Defined here as well as in start(), so anything touching it before a
        # browser exists — the parser tests, an early failure path — gets None
        # rather than an AttributeError.
        self._resale_response = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if config.OFFSCREEN and not self.headless:
            # Still a real headed browser as far as Chrome and the bot check
            # are concerned — just parked outside the visible desktop so it
            # isn't stealing focus every couple of minutes.
            args += ["--window-position=-2400,-2400"]
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=config.BROWSER_CHANNEL,
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="en-IE",
            timezone_id="Europe/Dublin",
            args=args,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(config.PAGE_TIMEOUT_MS)

        # Watch for the call that actually fills the resale panel. Polling the
        # rendered text for it is guesswork — it cannot distinguish "the panel
        # is still loading" from "the panel arrived and is empty", so a slow
        # response gets recorded as resale-blind. Roughly one poll in seven was
        # landing there. The network response is the fact; the DOM is its echo.
        self._resale_response = None
        self._page.on("response", self._note_resale_response)
        return self

    def _note_resale_response(self, response) -> None:
        try:
            if RESALE_API_RE.search(response.url):
                record = {"url": response.url, "status": response.status, "data": None}
                try:
                    # The body is the point: it carries the listings as data,
                    # before anything is drawn. Its own try because reading a
                    # body can fail if the response was already discarded.
                    record["data"] = response.json()
                except Exception:
                    pass
                self._resale_response = record
        except Exception:
            # A listener that raises would break the page, and this is only
            # ever an optimisation over reading the DOM.
            pass

    def close(self):
        for closer in (lambda: self._ctx.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._ctx = self._pw = self._page = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("BrowserSession used before start()")
        return self._page

    def reset_profile(self) -> bool:
        """Throw away the browser profile and start again with a clean one.

        Measured on 2026-08-13, and it changes what "blocked" means. After the
        watcher was rate-limited, moving to a completely different network did
        NOT clear it: the first request drew the ordinary 401 challenge, and
        the reload was still 403. A fresh profile on that same network worked
        immediately, first try.

        So the block lives in the profile's cookies — the bot-check tokens
        mark this *client*, and carrying them to a new IP just re-announces
        who you are. Changing address without changing identity is pointless.

        Safe to do: the watcher browses logged out, so the profile holds
        nothing but consent and bot-check state, both of which regenerate on
        the next page load.
        """
        import shutil

        self.close()
        target = self.profile_dir
        try:
            if target.exists():
                shutil.rmtree(target)
            print(f"[{stamp()}] browser profile reset — new identity, cookies cleared")
            ok = True
        except OSError as exc:
            print(f"[{stamp()}] could not reset profile {target}: {exc}")
            ok = False

        self.start()
        return ok

    def visible_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except PlaywrightError:
            return ""

    # ── getting to a real page ───────────────────────────────────────────────
    def _dismiss_consent(self) -> bool:
        """Accept the cookie dialog if it's up.

        Only ever needed once per profile, but it blocks the bot check from
        completing when it is up, so a run that skips it sees a blank page and
        concludes there are no tickets.
        """
        for name in CONSENT_BUTTONS:
            try:
                button = self.page.get_by_role("button", name=name, exact=True).first
                button.wait_for(state="visible", timeout=4_000)
                button.click()
                return True
            except (PlaywrightTimeout, PlaywrightError):
                continue
        return False

    def _load(self, reading: Reading, url: str = None) -> Optional[str]:
        """Navigate until we have the real event page. Returns normalised text.

        The 401-then-reload dance is expected, not exceptional — see the module
        docstring. Three attempts covers it comfortably.
        """
        last_status = None
        for attempt in range(1, 4):
            try:
                resp = self.page.goto(url or config.EVENT_URL, wait_until="domcontentloaded")
                last_status = resp.status if resp else None
            except PlaywrightTimeout:
                reading.note(f"attempt {attempt}: navigation timed out")
                continue
            except PlaywrightError as exc:
                reading.note(f"attempt {attempt}: navigation failed: {exc}")
                continue

            # 401 and 403 are different answers and must not be treated alike.
            # 401 is the ordinary challenge: solve it by reloading, which is
            # the documented happy path. 403 means this client is currently
            # blocked, and retrying immediately has never once cleared it —
            # it just triples the request volume at the exact moment something
            # upstream has decided we are asking too often. Bail out and let
            # the caller back off.
            # "Not found" is not a bot wall and must never be treated as one.
            # No amount of retrying, backing off or resetting the profile fixes
            # a URL that has changed — only a human editing config.py does. So
            # it stops here and is escalated in its own words.
            if last_status in (404, 410):
                reading.failed = True
                reading.page_gone = True
                reading.note(
                    f"HTTP {last_status} — Ticketmaster says this event page does "
                    "not exist. The URL has almost certainly changed; retrying "
                    "cannot fix that."
                )
                return None

            if last_status == 403:
                reading.failed = True
                reading.blocked = True
                reading.note(
                    "HTTP 403 — this client is rate-limited, not merely challenged. "
                    "Backing off instead of retrying."
                )
                return None

            self._dismiss_consent()
            try:
                self.page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeout:
                pass
            time.sleep(2)

            text = _normalise(self.visible_text())
            if any(marker in text for marker in REAL_PAGE_MARKERS):
                if attempt > 1:
                    reading.note(f"got the real page on attempt {attempt} (HTTP {last_status})")
                # The Find Tickets button paints before the ticket module
                # finishes, so "the button exists" is not "the page is ready".
                # Wait for the quantity stepper too, or the next step searches
                # a half-rendered page with the wrong quantity. Non-fatal: an
                # event with no tiers at all legitimately has no stepper.
                try:
                    self.page.get_by_role("spinbutton").first.wait_for(
                        state="visible", timeout=10_000
                    )
                except (PlaywrightTimeout, PlaywrightError):
                    reading.note("ticket module rendered no quantity stepper")
                return text
            reading.note(f"attempt {attempt}: HTTP {last_status}, bot-check page — reloading")

        reading.failed = True
        reading.note(
            f"never got past the bot check (last HTTP {last_status}). "
            "If this persists, run `calibrate` and look at the screenshot."
        )
        if self.headless:
            reading.note("NOTE: running headless, which this site always rejects — set EP_HEADLESS=0")
        return None

    # ── the search ───────────────────────────────────────────────────────────
    def _set_quantity(self, qty: int, reading: Reading) -> None:
        """Drive the quantity stepper to `qty`.

        It is a <span role="spinbutton"> with aria-valuenow/min/max, not a
        <select>, so it is driven with arrow keys the way a keyboard user
        would. Quantity is load-bearing: "there aren't enough tickets" is an
        answer about the number you asked for, and the page defaults to 2. Ask
        for 2 when you'd happily take 1 and you manufacture your own refusal.
        """
        try:
            spin = self.page.get_by_role("spinbutton").first
            spin.wait_for(state="visible", timeout=10_000)
        except (PlaywrightTimeout, PlaywrightError):
            reading.note("no quantity stepper found — using the page default")
            return

        try:
            lo = int(spin.get_attribute("aria-valuemin") or 1)
            hi = int(spin.get_attribute("aria-valuemax") or 4)
            target = max(lo, min(qty, hi))
            if target != qty:
                reading.note(f"wanted {qty}, clamped to the page's limit of {lo}-{hi}")

            if int(spin.get_attribute("aria-valuenow") or -1) == target:
                reading.note(f"quantity already {target}")
                return

            # press() focuses the element and sends a key without a real mouse
            # click. That matters: the page floats an overlay div over the
            # stepper which intercepts pointer events, so a click() here burns
            # its entire timeout and then gives up.
            for _ in range(12):  # bounded — the range is only ever 1..4
                current = int(spin.get_attribute("aria-valuenow") or target)
                if current == target:
                    break
                spin.press("ArrowDown" if current > target else "ArrowUp", timeout=8_000)
                time.sleep(0.3)
            reading.note(f"quantity set to {spin.get_attribute('aria-valuenow')}")
        except (PlaywrightTimeout, PlaywrightError, ValueError) as exc:
            reading.note(f"could not set quantity ({type(exc).__name__}) — using the page default")

    def _press_search(self, reading: Reading, qty: int) -> bool:
        # Cleared before every search, so a stale response from the previous
        # quantity or the previous event cannot be mistaken for this one's.
        self._resale_response = None
        try:
            button = self.page.get_by_role("button", name=SEARCH_BUTTONS).first
            button.wait_for(state="visible", timeout=15_000)
            button.click()
            reading.note(f"pressed search for {qty} ticket(s)")
            return True
        except (PlaywrightTimeout, PlaywrightError) as exc:
            reading.note(f"could not press the search button: {exc}")
            return False

    def _await_result(self, timeout_s: int = None) -> str:
        """Wait for the search to resolve into one of its three outcomes.

        The timeout defaults to config.search_timeout(), which is longer
        overnight — that is when every observed non-resolving search has
        happened, and each one costs a resale-blind poll. Waiting longer is
        paid only by searches that were going to fail: a healthy one returns
        as soon as its marker appears.
        """
        deadline = time.time() + (config.search_timeout() if timeout_s is None else timeout_s)
        while time.time() < deadline:
            if "checkout" in (self.page.url or "").lower():
                return "basket"
            text = _normalise(self.visible_text())
            if any(m in text for m in BASKET_MARKERS):
                return "basket"
            # "Search Again" replacing "Find Tickets" is the most reliable
            # marker that the search came back at all — it appears whether or
            # not there is a rejection message or a resale panel to show.
            if "search again" in text or NOT_ENOUGH in text or RESALE_MARKER in text:
                return "rejected"
            time.sleep(1.0)
        return "timeout"

    def _await_resale_panel(self, timeout_s: int = 25, render_s: float = 8.0,
                            settle_s: float = 2.0) -> tuple:
        """Wait until the resale panel can actually be READ. Returns (ok, why).

        The rejection message and the resale panel are two different responses:
        the search resolves, and only then does a separate call to
        /api/quickpicks/{event}/resale come back and render "Other Options".
        Parsing the moment the rejection appears therefore reads the page
        before resale has landed, and records a real listing as "no resale
        panel" — a missed alert on the one signal that matters most.

        The network response and the painted panel are not the same event,
        and treating them as one is what made this go wrong. Watching for the
        resale call is the right trigger, because it fires even when the panel
        is empty. But a Playwright `response` event fires when the response
        *headers* arrive, and the panel is rendered some way after that.
        Returning there meant the caller read the page before the panel
        existed and recorded a perfectly good poll as resale-blind: measured
        across 2026-08-16, that took the blind rate from 12% of polls to 30%.

        So the response is used for the thing it is genuinely good at —
        knowing how long to stay patient — and the DOM still decides. Once
        the call has answered, the panel gets `render_s` to paint, and if it
        does not, we stop and say so rather than spending the whole timeout on
        a page that was never going to show one.

        Waiting is the right currency to pay in. It costs nothing; a second
        search costs another request against the rate limit that blocked this
        client three times in a single day.
        """
        deadline = time.time() + timeout_s
        give_up_at = None

        while time.time() < deadline:
            # The success condition is *exactly* what _parse_resale keys on.
            # That agreement is deliberate. While the two disagreed — this
            # accepted a bare "Other Options", the parser demanded the
            # heading — a poll could be declared readable and then parsed as
            # resale-blind, with nothing in the log to explain the gap.
            if RESALE_HEADING in _normalise(self.visible_text()):
                return True, self._settle_resale(settle_s)

            if self._resale_response is not None and give_up_at is None:
                give_up_at = min(time.time() + render_s, deadline)
            if give_up_at is not None and time.time() >= give_up_at:
                status = (self._resale_response or {}).get("status")
                return False, (
                    f"the resale call answered (HTTP {status}) but the panel did "
                    f"not render within {render_s:.0f}s"
                )
            time.sleep(0.5)

        if self._resale_response is not None:
            return False, "the resale call answered but the panel never rendered"
        return False, (
            f"no resale call in {timeout_s}s — the search may not have completed"
        )

    def fetch_resale_json(self, event, qty: int) -> Optional[dict]:
        """Ask the resale endpoint directly, from inside the live page.

        The panel is the page's drawing of this call. Waiting for the drawing
        is what goes wrong: of the 80 resale-blind polls recorded to
        2026-08-19, 78 never saw the call at all — 26 of them after a search
        that had otherwise worked perfectly, because the page simply never
        made it inside the timeout. Nothing was wrong with the session; we
        were waiting for someone else to ask our question.

        So ask it. `page.evaluate` runs the fetch in the page's own context,
        carrying its cookies, its TLS fingerprint and its origin — Ticketmaster
        sees the call it already accepts from this page rather than a new
        client to be walled. That is the whole reason this can work where
        requests-from-Python cannot: the endpoint returns 403 to anything that
        is not a real browser session, and this IS the real browser session.

        The URL shape and its cache behaviour were confirmed from David's own
        signed-in browser on 2026-08-19:

            GET /api/quickpicks/{eventId}/resale?qty=1&offset=0&limit=20
            cache-control: max-age=15, stale-if-error=3600,
                           stale-while-revalidate=30

        `cache: no-store` is set so the browser revalidates rather than
        replaying its disk copy. A cache-busting query parameter would work
        too but is deliberately avoided: a novel URL misses Fastly's edge and
        hits origin, which is both heavier and more conspicuous than the call
        the page makes for itself.

        Returns a record in the same shape as the passive listener produces,
        so _parse_resale_json can read either without caring which. None if
        the call could not be made at all.
        """
        # config's tm_event_id first, then the id in the page's own URL.
        #
        # The fallback is not optional: only the standard page has
        # tm_event_id set, because that field exists for the Inventory Status
        # API and was filled in only where that API had been granted access.
        # Without deriving it here the rescue would silently never fire on the
        # instalment plan — which is the page whose longer interval makes each
        # blind poll cost more, not less.
        event_id = getattr(event, "tm_event_id", "") or _event_id_from_url(
            getattr(event, "url", "")
        )
        # getattr rather than self._page, matching _record_find. A subclass
        # that replaces only the Chrome-touching methods — which is how the
        # parsing tests exercise the real verdict logic — has no _page at all,
        # and this must degrade to "cannot fetch" rather than raising into the
        # middle of a poll.
        if not event_id or getattr(self, "_page", None) is None:
            return None
        url = (
            f"/api/quickpicks/{event_id}/resale"
            f"?qty={qty}&offset=0&limit=20"
        )
        try:
            record = self.page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        credentials: 'include',
                        cache: 'no-store',
                        headers: {'accept': 'application/json'},
                    });
                    let data = null;
                    try { data = await r.json(); } catch (e) { data = null; }
                    return {url: r.url, status: r.status, data: data};
                }""",
                url,
                # Its own budget. This is a rescue for a poll that has already
                # spent its patience, so it must not be able to double the
                # cost of a poll that was going to fail anyway.
                # (Playwright's default would be the page timeout.)
            )
        except (PlaywrightTimeout, PlaywrightError) as exc:
            return {"url": url, "status": None, "data": None,
                    "error": f"{type(exc).__name__}: {exc}"}
        return record

    def _settle_resale(self, settle_s: float) -> str:
        """Give the listing rows a moment to paint under the heading.

        The heading arrives before the rows do, so parsing on the heading
        alone can see an empty panel and record UNAVAILABLE for a page that is
        about to show a listing. That is the one error worse than being blind:
        UNKNOWN merely wastes the poll, while UNAVAILABLE is a confident wrong
        answer, and better_status() ranks it above UNKNOWN — so it would win
        the merge and be printed in the hourly email as fact.
        """
        deadline = time.time() + settle_s
        while time.time() < deadline:
            if _has_listing_rows(self.visible_text()):
                return "panel rendered with listing(s)"
            time.sleep(0.4)
        return "panel rendered"

    # ── reading the answer ───────────────────────────────────────────────────
    def _parse_resale(self, text: str, reading: Reading) -> None:
        """Find real resale listings, preferring the API response to the page.

        Detection is by the presence of listing entries, NOT by the absence of
        the "will appear below when they are available" caption — that caption
        is static and sits above real listings.

        The rendered page stays as a fallback for polls where the response was
        missed. A late answer beats no answer; it is only the *waiting* for
        rendering that cost coverage.
        """
        if _parse_resale_json(getattr(self, "_resale_response", None), reading):
            return
        raw = self.visible_text()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        listings: List[Listing] = []

        for i, line in enumerate(lines):
            if not _is_listing_row(line):
                continue
            section = None
            if i > 0:
                m = SECTION_RE.match(lines[i - 1])
                if m:
                    section = m.group(1).strip()

            price = None
            description = None
            for follow in lines[i + 1 : i + 4]:
                m = PRICE_RE.search(follow)
                if m and price is None:
                    price = f"€{m.group(1)}"
                elif description is None and not PRICE_RE.search(follow):
                    description = follow
            name = "Verified Resale"
            if section:
                name += f" — Section {section}"
            if description:
                name += f" ({description})"
            listings.append(Listing(name=name, price=price, kind="resale"))

        if listings:
            reading.resale = AVAILABLE
            reading.listings.extend(listings)
            reading.note(f"{len(listings)} verified-resale listing(s) on the page")
        elif RESALE_HEADING in text:
            reading.resale = UNAVAILABLE
            reading.note("resale panel rendered, no listings in it")
        else:
            reading.resale = UNKNOWN
            reading.note("no resale panel — the search may not have completed")

    def _parse_primary(self, outcome: str, text: str, reading: Reading, qty: int) -> None:
        if outcome == "basket":
            reading.primary = AVAILABLE
            reading.note("RESERVE ACCEPTED — tickets are held in a basket right now")
            for price in PRICE_RE.findall(text)[:1]:
                reading.listings.append(
                    Listing(name="General Admission (in basket)", price=f"€{price}", kind="primary")
                )
            return
        if outcome == "rejected":
            reading.primary = UNAVAILABLE
            if NOT_ENOUGH in text:
                reading.note(f"Ticketmaster refused {qty} ticket(s) — definitive no on primary")
            else:
                reading.note("search resolved without offering primary stock")
            return
        if any(hint in text for hint in SOLD_OUT_HINTS):
            reading.primary = UNAVAILABLE
            reading.note("explicit sold-out text")
            return
        reading.primary = UNKNOWN
        # Name the number it waited. "Within the timeout" gave no way to tell
        # whether a later change to that timeout had helped, which is exactly
        # the question asked after raising it overnight.
        reading.note(
            f"search did not resolve within {config.search_timeout()}s"
        )

    # ── the public call ──────────────────────────────────────────────────────
    def check(self, event=None) -> Reading:
        event = event or config.EVENTS[0]
        reading = Reading(
            source=SOURCE,
            event_slug=event.slug,
            event_name=event.name,
            event_url=event.url,
        )

        text = self._load(reading, event.url)
        if text is None:
            return reading

        if "sign out" not in text and "sign in" in text:
            # Worth knowing, but not a failure: the availability search works
            # logged out. Only buying needs the account.
            reading.note("browsing logged out (fine for watching, not for buying)")

        if not config.PRESS_THE_BUTTON:
            reading.primary = UNKNOWN
            reading.resale = UNKNOWN
            reading.note(
                "read-only mode: neither primary stock nor the resale panel is visible "
                "without searching, so this mode cannot answer the question"
            )
            return reading

        return self._search_quantities(reading, text, event)

    def _search_quantities(self, reading: Reading, text: str, event=None) -> Reading:
        """Search each wanted quantity, keeping the best answer found.

        Stops early on a basket, because at that point there is a live hold
        with a countdown and continuing to click things is the last thing we
        want to be doing.
        """
        event = event or config.EVENTS[0]
        searched = 0
        for index, qty in enumerate(config.WANTED_QUANTITIES):
            if index > 0:
                # Back to a clean page: after a search the button becomes
                # "Search Again" and the stepper may not be where we left it.
                # Load into a scratch reading — _load marks its argument failed
                # when it gives up, which would throw away a perfectly good
                # answer already collected from an earlier quantity.
                reload_probe = Reading(source=SOURCE)
                if self._load(reload_probe, event.url) is None:
                    reading.notes.extend(f"qty={qty}: {n}" for n in reload_probe.notes)
                    reading.note(f"could not reload for qty={qty} — ending the sweep here")
                    break
                time.sleep(2)

            self._set_quantity(qty, reading)
            if not self._press_search(reading, qty):
                continue
            searched += 1

            outcome = self._await_result()
            if outcome == "rejected":
                readable, why = self._await_resale_panel()
                # Say which of the two failures this was. "The call never came
                # back" and "the call came back and nothing painted" have
                # different fixes, and the old single note could not tell them
                # apart — so a resale-blind run left no evidence of its cause.
                if not readable:
                    reading.note(f"qty={qty}: resale unreadable — {why}")
            text = _normalise(self.visible_text())

            attempt = Reading(source=SOURCE)
            self._parse_primary(outcome, text, attempt, qty)
            self._parse_resale(text, attempt)

            # Last resort, and only when the page has left us blind. The
            # ordinary paths — the passive listener, then the rendered panel —
            # answer the great majority of polls, and this must not disturb
            # them: it runs only where the alternative is recording UNKNOWN
            # and learning nothing. The session is alive at this point (we
            # searched in it), so asking the endpoint ourselves costs one
            # small JSON call and converts a wasted poll into a real reading.
            if attempt.resale == UNKNOWN:
                rescue = self.fetch_resale_json(event, qty)
                if rescue and rescue.get("data") is not None:
                    self._resale_response = rescue
                    if _parse_resale_json(rescue, attempt):
                        attempt.note("resale read by asking the endpoint directly")
                elif rescue and rescue.get("status"):
                    attempt.note(
                        f"direct resale fetch answered HTTP {rescue['status']} "
                        f"with no usable body"
                    )
                elif rescue and rescue.get("error"):
                    attempt.note(f"direct resale fetch failed: {rescue['error']}")
            if attempt.any_good:
                self._record_find(attempt, qty)
            reading.notes.extend(f"qty={qty}: {n}" for n in attempt.notes)

            for listing in attempt.listings:
                if listing.describe() not in {l.describe() for l in reading.listings}:
                    reading.listings.append(listing)

            reading.primary = _better(reading.primary, attempt.primary)
            reading.resale = _better(reading.resale, attempt.resale)
            # Carried up, or a resale-endpoint 403 detected inside the sweep
            # would be reported in a note and nowhere else.
            reading.blocked = reading.blocked or attempt.blocked

            if outcome == "basket":
                reading.note(f"stopping the sweep at qty={qty} — there is a live basket")
                break

        if not searched:
            reading.failed = True
            reading.note("could not complete a single search")
        elif reading.primary == UNKNOWN and reading.resale == UNKNOWN:
            # Searched, and came away knowing nothing about either market.
            # That is a failed read, not a quiet "no tickets" — the same rule
            # the Inventory Status source already applies. Without this the
            # poll counts as a clean success, resets the failure counter, and
            # records UNKNOWN as though it were an answer.
            reading.failed = True
            reading.note(
                "searched, but learned nothing — primary and resale are both "
                "UNKNOWN. Treating as a failed read, not as 'no tickets'."
            )
        return reading

    # ── diagnostics ──────────────────────────────────────────────────────────
    def _record_find(self, reading: Reading, qty: int) -> None:
        """Keep everything about the one moment that cannot be reproduced.

        A find needs a real listing present while the watcher happens to be
        looking — it cannot be rehearsed, and by the next poll it is usually
        gone. So the whole state is written down at that instant: the API
        response with a populated `picks` (whose shape has never been seen,
        and which the parser currently guesses at), the rendered page, and a
        screenshot.

        Answering "what exactly did a real listing look like?" afterwards is
        otherwise impossible, and the answer decides how precisely alerts can
        describe the next one — possibly including whether there is an id to
        link straight to.
        """
        import json

        try:
            config.DIAG_DIR.mkdir(parents=True, exist_ok=True)
            base = config.DIAG_DIR / f"find-{time.strftime('%Y%m%d-%H%M%S')}-qty{qty}"

            record = getattr(self, "_resale_response", None)
            # Both the URL the browser is actually sitting on with the listing
            # visible, and the link the alert sent David to. The find of
            # 2026-08-18 showed these were the same string — Ticketmaster's
            # search changes page state without changing the address — which
            # is why the alert's link can only be a hypothesis until a live
            # find is opened from it. Recording both is what will settle it.
            from .. import notify
            payload = {
                "when": stamp(),
                "quantity_searched": qty,
                "url": self.page.url,
                "alert_link": notify.buy_url(
                    getattr(reading, "event_url", "") or self.page.url,
                    qty,
                    notify._best_listing(reading.listings),
                ),
                "primary": reading.primary,
                "resale": reading.resale,
                "listings": [l.describe() for l in reading.listings],
                "listing_ids": [l.listing_id for l in reading.listings if l.listing_id],
                "notes": list(reading.notes),
                "resale_api": record,
            }
            base.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str))
            base.with_suffix(".txt").write_text(self.visible_text())
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            print(f"[{stamp()}] find recorded: {base.with_suffix('.json')}")
        except Exception as exc:
            # Never let record-keeping cost the alert it is recording.
            print(f"[{stamp()}] could not record the find: {type(exc).__name__}: {exc}")

    def diagnose(self, label: str = "diag", search: bool = True) -> Path:
        """Dump screenshot + visible text + HTML, after searching by default.

        Use this whenever the anchors above stop matching — the post-search
        text is where every signal this module depends on actually lives.
        """
        config.DIAG_DIR.mkdir(parents=True, exist_ok=True)
        base = config.DIAG_DIR / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
        scratch = Reading(source="diagnose")
        if self._load(scratch) is not None and search:
            self._set_quantity(config.WANTED_QUANTITY, scratch)
            if self._press_search(scratch, config.WANTED_QUANTITY):
                self._await_result()
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".txt").write_text(self.visible_text())
            base.with_suffix(".html").write_text(self.page.content())
        except PlaywrightError as exc:
            print(f"[{stamp()}] diagnostic dump partly failed: {exc}")
        for note in scratch.notes:
            print(f"    {note}")
        return base


def check(event=None) -> Reading:
    """One-shot check. Cold-starts a browser; prefer `watch` for repeat polls."""
    with BrowserSession() as session:
        return session.check(event)
