"""Recording a find must be fast, complete, and must not photograph anything.

A find is the one moment where seconds decide whether a ticket is caught. On
2026-08-20 the recorder spent forty-five of them on a full-page screenshot
before timing out — the alert waited for it, and so did the securing attempt,
which then found the listing sold. That happened on six of seven finds that
day.

The screenshot was added when the resale response shape had never been seen
and the parser was guessing at it, and a picture was the only way to check the
guess. Seventeen finds later the shape is known and recorded in full, so the
question it existed to answer is answered permanently by a two-kilobyte JSON
file rather than a seven-hundred-kilobyte image that worked one time in three.

So the property here is negative, and negative properties are the ones that
quietly come back: the find path writes the data and takes no picture.
`calibrate` still does, because that is a human asking with no clock running.

Run with:  .venv/bin/python tests/test_find_record.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config  # noqa: E402
from ep_watcher.model import AVAILABLE, Listing, Reading, UNAVAILABLE  # noqa: E402
from ep_watcher.sources.browser import BrowserSession  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakePage:
    """Counts every attempt to photograph the page."""

    url = "https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping/event/X"

    def __init__(self):
        self.screenshots = 0

    def screenshot(self, **kw):
        self.screenshots += 1
        raise AssertionError("the find path must not take a screenshot")


class Recorder(BrowserSession):
    """Just enough of a session to exercise _record_find offline."""

    def __init__(self, page):
        self._page = page
        self._resale_response = {
            "url": "/api/quickpicks/X/resale",
            "status": 200,
            "data": {"picks": [{
                "id": "l5mm1z9t1s", "resaleListingId": "l5mm1z9t1s",
                "section": "STNDN1", "description": "WEEKEND CAMPING",
                "originalPrice": 366.39, "sellerBusinessType": "private",
            }], "total": 1},
        }

    @property
    def page(self):
        return self._page

    def visible_text(self):
        return "Verified Resale Tickets\nSection STNDN1\n€366.39\n"


def a_find():
    r = Reading(source="browser", primary=UNAVAILABLE, resale=AVAILABLE)
    r.event_slug = "weekend-camping"
    r.event_url = config.EVENTS[0].url
    r.listings.append(Listing("Verified Resale — Section STNDN1", "€366.39",
                              "resale", listing_id="l5mm1z9t1s", section="STNDN1"))
    return r


with tempfile.TemporaryDirectory() as tmp:
    was = config.DIAG_DIR
    config.DIAG_DIR = Path(tmp)
    try:
        page = FakePage()
        reading = a_find()
        Recorder(page)._record_find(reading, 1)

        print("\nIt takes no picture")
        check("zero screenshot attempts", page.screenshots, 0)

        print("\nIt writes the record that actually answers the question")
        written = sorted(p.suffix for p in Path(tmp).iterdir())
        check("a .json and a .txt, and nothing else", written, [".json", ".txt"])
        check("no .png at all", any(p.suffix == ".png" for p in Path(tmp).iterdir()), False)

        payload = json.loads(next(Path(tmp).glob("*.json")).read_text())
        print("\nAnd the JSON holds everything the screenshot was for")
        for field in ("when", "url", "alert_link", "primary", "resale",
                      "listings", "listing_ids", "notes", "resale_api"):
            check_true(f"it records {field}", field in payload)
        check("the listing id survives", payload["listing_ids"], ["l5mm1z9t1s"])
        pick = payload["resale_api"]["data"]["picks"][0]
        check("and the full API record with it", pick["section"], "STNDN1")
        check_true("including the price the alert quotes", pick["originalPrice"] == 366.39)

        print("\nRecording never raises into the poll that is mid-find")
        # It is wrapped for a reason: this runs inside a poll that has just
        # found a ticket, and record-keeping must never cost the alert it is
        # keeping a record of.
        class Hostile(Recorder):
            def visible_text(self):
                raise RuntimeError("the page went away")

        try:
            Hostile(FakePage())._record_find(a_find(), 1)
            raised = False
        except Exception:
            raised = True
        check("a broken page does not propagate", raised, False)
    finally:
        config.DIAG_DIR = was

print("\nThe manual diagnostic still photographs, because nobody is racing")
import inspect  # noqa: E402
source = inspect.getsource(BrowserSession.diagnose)
check_true("calibrate still takes a screenshot", "screenshot" in source)
check_true("and a full-page one, since there is time for it",
           "full_page=True" in source)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
