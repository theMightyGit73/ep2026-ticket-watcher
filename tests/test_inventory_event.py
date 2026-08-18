"""The Inventory Status source must never answer about the wrong page.

It used to take no event at all and always query config.TM_EVENT_ID — the
standard Weekend Camping page — while engine.poll() stamped the answer with
whichever page was being polled. With two pages watched, that means the
instalment plan inherits the standard page's inventory, and an AVAILABLE there
sends David to a page with nothing on it while the real listing sells.

Dormant only because no access grant has arrived: the source reports itself
unconfigured without TM_API_KEY. It would have fired on the first day one did.
This is the same wrong-page failure that alerts, watchdog reasons and hourly
reports were each fixed for in turn; this was the last place it still lived.

Run with:  .venv/bin/python tests/test_inventory_event.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config  # noqa: E402
from ep_watcher.sources import inventory_api  # noqa: E402

failures = []
requested = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return [{"eventId": "SOME-ID", "status": "TICKETS_AVAILABLE",
                 "resaleStatus": "TICKETS_NOT_AVAILABLE"}]


def fake_get(url, params=None, timeout=None):
    requested.append(params.get("events"))
    return FakeResponse()


inventory_api.requests = type("_R", (), {"get": staticmethod(fake_get),
                                         "RequestException": Exception})()
config.TM_API_KEY = "test-key"

standard, instalment = config.EVENTS[0], config.EVENTS[-1]

print("\nEach event is asked about by its own id")

check_true("the standard page has an id", bool(standard.tm_event_id))
requested.clear()
reading = inventory_api.check(standard)
check("it is the one that gets queried", requested, [standard.tm_event_id])
check("and the answer is labelled with that page", reading.event_slug, standard.slug)

print("\nA page with no id of its own gets silence, not someone else's answer")

check("the instalment page has no id yet", instalment.tm_event_id, "")
requested.clear()
reading = inventory_api.check(instalment)
check("no request is made at all", requested, [])
check("the source reports itself unable to answer", reading.failed, True)
check("it is still labelled with the right page", reading.event_slug, instalment.slug)
check_true("and says why", any("no Inventory Status id" in n for n in reading.notes))
check("crucially, it claims nothing about stock", reading.primary, "UNKNOWN")

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
