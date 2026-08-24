"""A reminder must not shout as loudly as news.

Added 2026-08-24. On that day the watcher sent 69 pushes for 16 listings —
roughly four apiece — and every one of them was titled the same way, carried
the same urgent priority, and where the phone was configured, rang it. An
alert that arrives four times is not four times as loud; it is the alert
teaching its reader to ignore it.

That matters more here than it would anywhere else. The buyer has secured
none of the 65 listings it has attempted, so the plan now rests on David
seeing an alert and buying by hand. The notification IS the product. A
lock-screen title that cannot distinguish "a ticket just appeared" from "the
same ticket is still there" spends the one thing the whole system exists to
deliver.

The rule pinned here: a listing he has not been told about keeps everything —
urgent priority, the ring, the wording. A repeat says so in its first word and
arrives quietly, while still carrying the link, because a quiet reminder he
happens to see is still a ticket he can buy.

Run with:  .venv/bin/python tests/test_alert_repeats.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, notify  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def reading():
    ev = config.EVENTS[0]
    return Reading(
        primary=UNAVAILABLE,
        resale=AVAILABLE,
        source="browser",
        listings=[Listing(
            name="Verified Resale — Section STNDN1 (WEEKEND CAMPING)",
            price="€366.39",
            kind="resale",
            listing_id="l0vmtvwkd2",
        )],
        event_slug=ev.slug,
        event_name=ev.name,
        event_url=ev.url,
    )


# Capture what would have gone out, instead of sending it.
sent = {"push": [], "email": [], "rings": 0}

notify.TEST_MODE = False  # the ring is suppressed in test mode; test it directly


def fake_push(_label, **kw):
    sent["push"].append(kw)


def fake_email(subject, body):
    sent["email"].append((subject, body))


def fake_ring(what):
    sent["rings"] += 1


notify._push = fake_push
notify._send_email = fake_email
notify.ring_phone = fake_ring
notify._safe = lambda _label, fn, *a, **k: fn(*a, **k)


print("\nA new listing is news")

sent = {"push": [], "email": [], "rings": 0}
notify.available(reading(), "new listing", ["Verified Resale — STNDN1 — €366.39"])

push = sent["push"][0]
check("the push is urgent", push["priority"], "urgent")
check("the title says NEW", push["title"].startswith("NEW"), True)
check("the subject shouts", "TICKETS AVAILABLE" in sent["email"][0][0], True)
check("and the phone rings", sent["rings"], 1)
check("the link is carried", bool(push.get("click")), True)


print("\nThe same listing again is not")

sent = {"push": [], "email": [], "rings": 0}
notify.available(reading(), "still available — reminder (4 min)", [])

push = sent["push"][0]
check("the push drops off urgent", push["priority"], "default")
check("the title says so first", push["title"].startswith("still there"), True)
check("the subject does not shout",
      "TICKETS AVAILABLE" in sent["email"][0][0], False)
check("the phone does not ring", sent["rings"], 0)

# The quiet one still has to be actionable. A reminder he happens to glance at
# is a ticket he can still buy, and stripping the link would make it purely
# noise — which is the opposite of the fix.
check("but the link is still carried", bool(push.get("click")), True)
check("and the email still names the event",
      config.EVENTS[0].name in sent["email"][0][1], True)


print("\nBoth kinds tell him not to wait for the watcher")

# While the buyer is refused on every listing, an alert that implies securing
# is coming is actively harmful — it buys hesitation at the one moment
# hesitation costs the ticket.
for label, new in (("new", ["x"]), ("repeat", [])):
    sent = {"push": [], "email": [], "rings": 0}
    notify.available(reading(), "trigger", new)
    body = sent["email"][0][1]
    check(f"the {label} email says to buy by hand",
          "do not wait for the watcher" in body.lower(), True)


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All alert-repeat checks passed.")
