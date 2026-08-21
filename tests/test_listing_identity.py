"""What makes two sightings the same listing — and why it is not the id.

This file exists to stop a plausible, tempting, wrong change.

The reasoning that leads to it goes: Ticketmaster gives every resale listing
an id, `pending_listings` compares the human-readable description instead, and
the description of a Weekend Camping listing is byte-identical every time
("Verified Resale — Section STNDN1 (WEEKEND CAMPING) — €366.39"). So a
genuinely new ticket at the same price cannot look new, and the obvious fix is
to key identity on the id. It was recommended in as many words during the
review of 2026-08-21.

It is wrong, and the project's own event log is what settles it. On 2026-08-20
the Early Entry page reported a resale listing at 15:08:59 with id
`lfsqh34dh`, and reported it again at 15:15:25 with id `lhzrxpxk` — six and a
half minutes later, same section, same price. The second reading's own recorded
reason was "still available — reminder (4 min)", which is only reachable when
resale never went UNAVAILABLE in between and nothing looked new. One
continuously-listed ticket, two ids.

So keying on the id would make an unsold listing look new on every single poll
and re-alert for as long as it sat there — which is precisely the failure the
comment on Listing.listing_id was written to prevent, arrived at by the exact
route it warned about.

The real problem behind the recommendation was real: at 20:04 and 20:06 on
2026-08-20 there was live stock and no second securing attempt. But identity
was never the cause. Securing shared the alerting clock, and that is fixed in
state.should_try_again — see tests/test_secure_clock.py.

Run with:  .venv/bin/python tests/test_listing_identity.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


SLUG = config.EVENTS[0].slug


def seen_as(listing_id):
    """The same listing, as the feed reported it on two consecutive polls."""
    r = Reading(source="resale-sweep")
    r.event_slug = SLUG
    r.event_name = config.EVENTS[0].name
    r.primary = UNAVAILABLE
    r.resale = AVAILABLE
    r.listings.append(
        Listing("Verified Resale — Section EARLY (EARLY ENTRY)", "€46.50",
                "resale", listing_id=listing_id, section="EARLY"))
    return r


print("\nThe id is not part of what makes a listing recognisable")
first, again = seen_as("lfsqh34dh"), seen_as("lhzrxpxk")
check("two ids, one description",
      first.listings[0].describe(), again.listings[0].describe())
check_true("and the ids really do differ",
           first.listings[0].listing_id != again.listings[0].listing_id)


print("\nSo the same listing reported under a new id is not 'new'")
state = dict(st._defaults())
new = st.pending_listings(state, first)
check("the first sighting is new", len(new), 1)
st.record_success(state, first)

check("the same listing under a different id is not",
      st.pending_listings(state, again), [])
should, reason = st.should_alert_availability(
    state, again, st.pending_listings(state, again))
check("so nothing claims a new listing appeared",
      "new listing" in reason, False)


print("\nA genuinely different listing still registers")
# The guard must not have turned the diff into a blanket "never new". A
# different section, or a different price, is a different ticket.
other = seen_as("lfsqh34dh")
other.listings[0] = Listing(
    "Verified Resale — Section STNDN1 (WEEKEND CAMPING)", "€366.39",
    "resale", listing_id="lfsqh34dh", section="STNDN1")
check("a different section is a new listing",
      len(st.pending_listings(state, other)), 1)

cheaper = seen_as("lfsqh34dh")
cheaper.listings[0] = Listing(
    "Verified Resale — Section EARLY (EARLY ENTRY)", "€30.00",
    "resale", listing_id="lfsqh34dh", section="EARLY")
check("and so is a different price",
      len(st.pending_listings(state, cheaper)), 1)


print("\nThe id is still carried — it answers a different question")
# "Which listing is this right now" is what the id is for: pointing an alert
# at a specific listing, and letting _probe_after_gone say whether the ticket
# it just failed on is the one the feed still lists. That is worth having and
# is not identity across time.
check("the find record can still name it",
      [l.listing_id for l in again.listings], ["lhzrxpxk"])
check("but describe() does not leak it into the diff",
      "lhzrxpxk" in again.listings[0].describe(), False)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
