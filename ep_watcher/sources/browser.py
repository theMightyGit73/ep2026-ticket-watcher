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
BASKET_MARKERS = ("time left to complete", "your tickets are reserved", "proceed to checkout")
SOLD_OUT_HINTS = ("tickets are currently unavailable", "this event is sold out")

PRICE_RE = re.compile(r"€\s?(\d{1,4}[.,]\d{2})")
SECTION_RE = re.compile(r"^section\s+(.+)$", re.I)


def _normalise(text: str) -> str:
    """Lowercase and drop apostrophes so curly-vs-straight can't break a match."""
    return re.sub(r"[’'`]", "", text or "").lower()


class BrowserSession:
    """A warm Chrome, intended to be held open across many polls."""

    def __init__(self, headless: Optional[bool] = None):
        self.headless = config.HEADLESS if headless is None else headless
        self._pw = None
        self._ctx = None
        self._page = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def start(self):
        config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if config.OFFSCREEN and not self.headless:
            # Still a real headed browser as far as Chrome and the bot check
            # are concerned — just parked outside the visible desktop so it
            # isn't stealing focus every couple of minutes.
            args += ["--window-position=-2400,-2400"]
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            channel=config.BROWSER_CHANNEL,
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="en-IE",
            timezone_id="Europe/Dublin",
            args=args,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(config.PAGE_TIMEOUT_MS)
        return self

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

    def _load(self, reading: Reading) -> Optional[str]:
        """Navigate until we have the real event page. Returns normalised text.

        The 401-then-reload dance is expected, not exceptional — see the module
        docstring. Three attempts covers it comfortably.
        """
        last_status = None
        for attempt in range(1, 4):
            try:
                resp = self.page.goto(config.EVENT_URL, wait_until="domcontentloaded")
                last_status = resp.status if resp else None
            except PlaywrightTimeout:
                reading.note(f"attempt {attempt}: navigation timed out")
                continue
            except PlaywrightError as exc:
                reading.note(f"attempt {attempt}: navigation failed: {exc}")
                continue

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
        try:
            button = self.page.get_by_role("button", name=SEARCH_BUTTONS).first
            button.wait_for(state="visible", timeout=15_000)
            button.click()
            reading.note(f"pressed search for {qty} ticket(s)")
            return True
        except (PlaywrightTimeout, PlaywrightError) as exc:
            reading.note(f"could not press the search button: {exc}")
            return False

    def _await_result(self, timeout_s: int = 45) -> str:
        """Wait for the search to resolve into one of its three outcomes."""
        deadline = time.time() + timeout_s
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

    def _await_resale_panel(self, timeout_s: int = 12) -> bool:
        """Wait for the resale panel, which arrives after the rejection does.

        The rejection message and the resale panel are two different responses:
        the search resolves, and only then does a separate call to
        /api/quickpicks/{event}/resale come back and render "Other Options".
        Parsing the moment the rejection appears therefore reads the page
        before resale has landed, and records a real listing as "no resale
        panel" — a missed alert on the one signal that matters most.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            text = _normalise(self.visible_text())
            if RESALE_HEADING in text or "other options" in text:
                return True
            time.sleep(0.5)
        return False

    # ── reading the answer ───────────────────────────────────────────────────
    def _parse_resale(self, text: str, reading: Reading) -> None:
        """Find real resale listings in the post-search panel.

        Detection is by the presence of listing entries, NOT by the absence of
        the "will appear below when they are available" caption — that caption
        is static and sits above real listings.
        """
        raw = self.visible_text()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        listings: List[Listing] = []

        for i, line in enumerate(lines):
            if line.lower() != RESALE_MARKER:
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
        reading.note("search did not resolve within the timeout")

    # ── the public call ──────────────────────────────────────────────────────
    def check(self) -> Reading:
        reading = Reading(source=SOURCE)

        text = self._load(reading)
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

        return self._search_quantities(reading, text)

    def _search_quantities(self, reading: Reading, text: str) -> Reading:
        """Search each wanted quantity, keeping the best answer found.

        Stops early on a basket, because at that point there is a live hold
        with a countdown and continuing to click things is the last thing we
        want to be doing.
        """
        searched = 0
        for index, qty in enumerate(config.WANTED_QUANTITIES):
            if index > 0:
                # Back to a clean page: after a search the button becomes
                # "Search Again" and the stepper may not be where we left it.
                # Load into a scratch reading — _load marks its argument failed
                # when it gives up, which would throw away a perfectly good
                # answer already collected from an earlier quantity.
                reload_probe = Reading(source=SOURCE)
                if self._load(reload_probe) is None:
                    reading.notes.extend(f"qty={qty}: {n}" for n in reload_probe.notes)
                    reading.note(f"could not reload for qty={qty} — ending the sweep here")
                    break
                time.sleep(2)

            self._set_quantity(qty, reading)
            if not self._press_search(reading, qty):
                continue
            searched += 1

            outcome = self._await_result()
            if outcome == "rejected" and not self._await_resale_panel():
                reading.note(f"qty={qty}: resale panel never arrived")
            text = _normalise(self.visible_text())

            attempt = Reading(source=SOURCE)
            self._parse_primary(outcome, text, attempt, qty)
            self._parse_resale(text, attempt)
            reading.notes.extend(f"qty={qty}: {n}" for n in attempt.notes)

            for listing in attempt.listings:
                if listing.describe() not in {l.describe() for l in reading.listings}:
                    reading.listings.append(listing)

            reading.primary = _better(reading.primary, attempt.primary)
            reading.resale = _better(reading.resale, attempt.resale)

            if outcome == "basket":
                reading.note(f"stopping the sweep at qty={qty} — there is a live basket")
                break

        if not searched:
            reading.failed = True
            reading.note("could not complete a single search")
        return reading

    # ── diagnostics ──────────────────────────────────────────────────────────
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


def check() -> Reading:
    """One-shot check. Cold-starts a browser; prefer `watch` for repeat polls."""
    with BrowserSession() as session:
        return session.check()
