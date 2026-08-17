"""Reading resale listings from the API response instead of the rendered page.

The page fetches its listings as JSON and then draws them. Reading the JSON
is reading the fact; reading the page is reading its echo, and the echo
arrives late. Waiting for it could not tell "not drawn yet" from "drawn and
empty", so about a quarter of polls were recorded as resale-blind — polls
that could not have seen a listing even if one had been sitting there.

Shape captured live on 2026-08-18, with an empty panel:

    {"quantity": 0, "total": 0, "picks": [], "descriptions": []}

A populated `picks` entry has never been observed, because that needs a
listing present at the moment someone is looking. So these checks pin the
behaviour that matters under that ignorance: an unrecognised entry must still
count as a listing. Describing it badly costs a line of text; missing it
costs the ticket.

Run with:  .venv/bin/python tests/test_resale_json.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher.model import AVAILABLE, UNAVAILABLE, Reading  # noqa: E402
from ep_watcher.sources import browser  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def parse(data, status=200):
    """Run the parser over a captured response record.

    A plain function taking the record, so this needs no browser and no
    session stub — which is the reason it was pulled out of the class.
    """
    record = None if data is None else {"url": "x/resale", "status": status, "data": data}
    reading = Reading(source="t")
    answered = browser._parse_resale_json(record, reading)
    return answered, reading


print("\nThe empty response is a definitive no, with no rendering involved")

EMPTY = {"quantity": 0, "total": 0, "picks": [], "descriptions": []}
answered, reading = parse(EMPTY)
check("it answers", answered, True)
check("nothing available", reading.resale, UNAVAILABLE)
check("and no listings invented", len(reading.listings), 0)
check_true("says it was definitive", any("definitive" in n for n in reading.notes))

print("\nA populated response is a find")

one = {"quantity": 1, "total": 1, "picks": [
    {"section": "STNDN2", "row": "GA", "description": "WEEKEND CAMPING", "price": 366.39}
]}
answered, reading = parse(one)
check("it answers", answered, True)
check("available", reading.resale, AVAILABLE)
check("one listing", len(reading.listings), 1)
listing = reading.listings[0]
check_true("names the section", "STNDN2" in listing.name)
check_true("keeps the description", "WEEKEND CAMPING" in listing.name)
check("formats the price", listing.price, "€366.39")
check("marked as resale", listing.kind, "resale")

print("\nSeveral listings")

many = {"total": 3, "picks": [
    {"section": "A", "price": 300},
    {"section": "B", "price": 350.5},
    {"section": "C", "price": 400},
]}
answered, reading = parse(many)
check("all three", len(reading.listings), 3)
check("integer prices still format", reading.listings[0].price, "€300.00")
check("and fractional ones", reading.listings[1].price, "€350.50")

print("\nAn unfamiliar schema must never lose a listing")
# The real shape of a pick is unknown. Whatever comes back, the count is what
# decides — a listing described badly is still a listing found.

odd = {"total": 2, "picks": [{"mysteryField": "???"}, {"another": 1}]}
answered, reading = parse(odd)
check("still available", reading.resale, AVAILABLE)
check("still two listings", len(reading.listings), 2)
check_true("logs the keys so the schema can be learned",
           any("pick keys" in n for n in reading.notes))

count_only = {"total": 2, "picks": []}
answered, reading = parse(count_only)
check("a count with no detail is still a find", reading.resale, AVAILABLE)
check_true("with a placeholder listing", len(reading.listings) >= 1)

nested = {"total": 1, "picks": [{"section": {"name": "STNDN9"}, "price": {"value": 366.39}}]}
answered, reading = parse(nested)
check_true("nested objects are unwrapped", "STNDN9" in reading.listings[0].name)

print("\nWhen it cannot answer, it says so and lets the page decide")

check("no response captured at all", parse(None)[0], False)
check("a non-200 response", parse(EMPTY, status=403)[0], False)
check("an unrecognised body", parse({"foo": "bar"})[0], False)
check("a list instead of an object", parse([])[0], False)

answered, reading = parse(EMPTY, status=403)
check_true("and explains the fallback", any("falling back" in n for n in reading.notes))

print("\nA total that disagrees with picks trusts the total")
# total is the count the page itself uses; picks may be paginated.
answered, reading = parse({"total": 5, "picks": [{"section": "A"}]})
check("reports the real count", any("5 listing" in n for n in reading.notes), True)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
