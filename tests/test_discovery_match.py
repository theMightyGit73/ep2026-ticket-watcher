"""Check which indexed events count as "the ticket David wants".

This matters more than it looks. The Discovery source works by absence: the
Weekend Camping event is missing from the index while it is sold out, so its
*reappearance* is the signal. Two ways that goes wrong:

  * Match too loosely and the two campervan passes — permanently indexed —
    look like a ticket, so it alerts forever and you stop believing it.
  * Match too tightly and the real event comes back and nothing fires.

Run with:  .venv/bin/python tests/test_discovery_match.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher.sources.discovery import _is_wanted_event  # noqa: E402

failures = []


def check(name, want):
    got = _is_wanted_event({"name": name})
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {'match ' if want else 'ignore'}: {name!r} -> {got}")
    if not ok:
        failures.append(name)


print("\nMust be ignored — these are indexed permanently and are not the ticket")
# Verified present in the live index on 2026-08-13.
check("Electric Picnic 2026 - Campervan/Caravan Pass", False)
check("Electric Picnic 2026 - Family Campervan/Caravan Pass", False)
check("Bon Jovi", False)
check("Electric Picnic (Australia)", False)

print("\nMust match — the wanted ticket, however they word it")
# Exact name from the ticketmaster.ie page title.
check("Electric Picnic 2026 - Weekend Camping", True)
check("Electric Picnic 2026 – Weekend Camping Ticket", True)
check("ELECTRIC PICNIC 2026 - WEEKEND CAMPING", True)
check("Electric Picnic 2026 - Weekend Ticket", True)

print("\nEdge cases")
check("", False)
check("Weekend Camping", False)  # not identified as Electric Picnic

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
