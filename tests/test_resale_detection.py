"""Knowing whether the resale panel actually arrived.

The panel is filled by its own API call, separate from the search response.
Waiting for it by polling the rendered text is guesswork: "no panel yet" and
"panel arrived, nothing in it" look identical for as long as the call is in
flight. On a phone hotspot that gap was wide enough to record roughly one
poll in seven as resale-blind — a poll that could not have seen a listing
even if one was there.

Watching for the network response settles it. These checks cover the
matching, the reset between searches, and the rule that matters most: an
unread panel is UNKNOWN, never "no tickets".

Run with:  .venv/bin/python tests/test_resale_detection.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher.model import UNKNOWN, Reading  # noqa: E402
from ep_watcher.sources import browser  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


print("\nRecognising the call that fills the resale panel")

# Captured verbatim from the live page on 2026-08-13.
real = ("https://www.ticketmaster.ie/api/quickpicks/18006314BD813D3E/resale"
        "?qty=1&offset=0&limit=20")
check("the real resale call matches", bool(browser.RESALE_API_RE.search(real)), True)

instalment = "https://www.ticketmaster.ie/api/quickpicks/18006314CFB4A99E/resale?qty=1"
check("and the instalment page's", bool(browser.RESALE_API_RE.search(instalment)), True)
check("without a query string too",
      bool(browser.RESALE_API_RE.search("/api/quickpicks/ABC123/resale")), True)

print("\nIt must not match the other calls the search makes")

for other in (
    "https://checkout.ticketmaster.ie/graphql",
    "https://checkout.ticketmaster.ie/api/rules?eventId=18006314BD813D3E",
    "https://pubapi.ticketmaster.ie/logger/log",
    "https://www.ticketmaster.ie/api/quickpicks/18006314BD813D3E",   # no /resale
):
    check(f"ignores {other.split('/')[-1][:28]}",
          bool(browser.RESALE_API_RE.search(other)), False)


class StubResponse:
    def __init__(self, url, status=200):
        self.url = url
        self.status = status


def fresh_session():
    """A session object without a browser behind it."""
    return browser.BrowserSession.__new__(browser.BrowserSession)


print("\nThe flag exists before a browser does")

s = browser.BrowserSession()
check("constructed sessions start with no response", s._resale_response, None)

print("\nRecording a response")

s._note_resale_response(StubResponse(real))
check("a resale call is recorded", s._resale_response is not None, True)
check("with its status", s._resale_response["status"], 200)

s._resale_response = None
s._note_resale_response(StubResponse("https://checkout.ticketmaster.ie/graphql"))
check("an unrelated call is not", s._resale_response, None)

print("\nA broken response object must not break the page")

class Exploding:
    @property
    def url(self):
        raise RuntimeError("boom")

s._note_resale_response(Exploding())
check("the listener swallows it", s._resale_response, None)

print("\nAn unread panel is UNKNOWN, never 'no tickets'")


class FakePage:
    def __init__(self, text):
        self.text = text

    def visible_text(self):
        return self.text


# Search resolved, resale panel never rendered.
text = "There aren't enough tickets to complete your request\nSearch Again"
reading = Reading(source="test")
browser.BrowserSession._parse_resale(FakePage(text), browser._normalise(text), reading)
check("unread panel stays unknown", reading.resale, UNKNOWN)
check("and is not good news", reading.any_good, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
