"""Source: the free Ticketmaster Discovery API.

This is the only source that works with no browser at all, which makes it the
only one that can run somewhere other than a machine with a real Chrome on it.
Free self-signup key, 5000 calls/day, 5 requests/second, and Ireland is a
supported country. No bot detection: it is a documented public API rather than
a page being scraped.

  https://app.ticketmaster.com/discovery/v2/

Three signals, in descending order of how much they can be trusted:

  1. TMR events. "tmr" is the Ticketmaster Resale platform and is a documented
     value of the `source` parameter. If resale inventory for this event
     surfaces in Discovery as a tmr-sourced event, that is a real resale
     signal available for free from anywhere. UNVERIFIED — it needs an API key
     to test, and the answer decides whether cloud hosting can watch resale.

  2. priceRanges. Documented as the range over *available* inventory, so it
     appearing on an event that had none is a genuine change. Ticketmaster
     documents price data as refreshed at most hourly, and only guarantees the
     feature in US/CA/AU/NZ/MX — so on an IE event treat its presence as a
     hint worth checking, never as proof.

  3. dates.status.code. Flips onsale/offsale. Coarse: a sold-out festival that
     gets one ticket back will almost certainly stay "onsale" throughout, so
     this catches a general re-release and nothing smaller.

None of these can see what the browser sees. A single Verified Resale listing
appearing and vanishing within five minutes — which is the behaviour actually
observed on this event — is below the resolution of every one of them. This
source is a cheap, always-on safety net, not a replacement for the browser.
"""

from typing import List, Optional

import requests

from .. import config
from ..model import AVAILABLE, UNAVAILABLE, UNKNOWN, Listing, Reading

SOURCE = "discovery-api"

#: Event statuses that mean tickets are notionally on sale.
_ONSALE = {"onsale"}
_OFFSALE = {"offsale", "cancelled", "postponed", "rescheduled"}


def configured() -> bool:
    return bool(config.DISCOVERY_KEY)


def _get(path: str, **params) -> Optional[dict]:
    params["apikey"] = config.DISCOVERY_KEY
    resp = requests.get(f"{config.DISCOVERY_ROOT}{path}", params=params, timeout=20)
    if resp.status_code == 401:
        raise PermissionError("Discovery rejected the API key (401 Invalid ApiKey)")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def check() -> Reading:
    reading = Reading(source=SOURCE)

    if not configured():
        reading.failed = True
        return reading.note("TM_DISCOVERY_KEY not set — source skipped")

    try:
        event = _get(f"/events/{config.TM_EVENT_ID}.json")
    except PermissionError as exc:
        reading.failed = True
        return reading.note(str(exc))
    except requests.RequestException as exc:
        reading.failed = True
        return reading.note(f"request failed: {exc}")

    if event is None:
        reading.failed = True
        return reading.note(
            f"no Discovery event with id {config.TM_EVENT_ID} — run `resolve-id` "
            "to find the id Discovery knows this event by"
        )

    _read_status(event, reading)
    _read_price_ranges(event, reading)
    _read_resale(event, reading)
    return reading


def _read_status(event: dict, reading: Reading) -> None:
    code = (event.get("dates", {}).get("status", {}) or {}).get("code", "")
    reading.note(f"event status: {code or 'unknown'}")
    if code in _OFFSALE:
        reading.primary = UNAVAILABLE
    elif code in _ONSALE:
        # "onsale" is not "buyable" — this event has read onsale throughout a
        # period when the checkout refused every request. Deliberately not
        # promoted to AVAILABLE, or the watcher would alert forever.
        reading.primary = UNKNOWN
        reading.note("status is onsale, which for this event does not imply purchasable")
    else:
        reading.primary = UNKNOWN


def _read_price_ranges(event: dict, reading: Reading) -> None:
    ranges = event.get("priceRanges") or []
    if not ranges:
        reading.note("no priceRanges (expected: IE is outside the documented markets)")
        return
    for pr in ranges:
        reading.note(
            f"priceRange {pr.get('type', '?')}: "
            f"{pr.get('min')}–{pr.get('max')} {pr.get('currency', '')}"
        )


def _read_resale(event: dict, reading: Reading) -> None:
    """Look for resale inventory surfaced as tmr-sourced events."""
    try:
        found = find_resale_events()
    except (requests.RequestException, PermissionError) as exc:
        reading.note(f"resale lookup failed: {exc}")
        reading.resale = UNKNOWN
        return

    local_date = (event.get("dates", {}).get("start", {}) or {}).get("localDate")
    same_day = [e for e in found if e.get("date") == local_date] if local_date else found

    if same_day:
        reading.resale = AVAILABLE
        for e in same_day:
            reading.listings.append(
                Listing(name=f"TMR resale event: {e['name']}", price=e.get("price"), kind="resale")
            )
        reading.note(f"{len(same_day)} tmr-sourced event(s) matching {local_date}")
    elif found:
        reading.resale = UNAVAILABLE
        reading.note(f"{len(found)} tmr event(s) found, none on {local_date}")
    else:
        reading.resale = UNAVAILABLE
        reading.note("no tmr-sourced events for Electric Picnic in IE")


def find_resale_events() -> List[dict]:
    """Search Discovery for Ticketmaster Resale events for this festival."""
    payload = _get(
        "/events.json",
        keyword="Electric Picnic",
        countryCode="IE",
        source="tmr",
        size=50,
    )
    events = ((payload or {}).get("_embedded", {}) or {}).get("events", []) or []
    return [
        {
            "id": e.get("id"),
            "name": e.get("name"),
            "date": (e.get("dates", {}).get("start", {}) or {}).get("localDate"),
            "price": _first_price(e),
        }
        for e in events
    ]


def _first_price(event: dict) -> Optional[str]:
    ranges = event.get("priceRanges") or []
    if not ranges:
        return None
    pr = ranges[0]
    return f"{pr.get('min')}–{pr.get('max')} {pr.get('currency', '')}".strip()


# ── Setup helper ─────────────────────────────────────────────────────────────

def search_events(keyword: str = "Electric Picnic", country: str = "IE") -> List[dict]:
    """List every Discovery event matching the festival, whatever the source.

    Used by `resolve-id`. The id in the ticketmaster.ie URL is a host id;
    Discovery's example ids are the same 16-hex-character shape, so it may
    work directly — but it is not guaranteed, and guessing is how you end up
    staring at an empty result.
    """
    payload = _get("/events.json", keyword=keyword, countryCode=country, size=50)
    events = ((payload or {}).get("_embedded", {}) or {}).get("events", []) or []
    return [
        {
            "id": e.get("id"),
            "name": e.get("name"),
            "date": (e.get("dates", {}).get("start", {}) or {}).get("localDate"),
            "status": (e.get("dates", {}).get("status", {}) or {}).get("code"),
            "source": (e.get("_embedded", {}) or {}).get("source") or e.get("source"),
            "url": e.get("url"),
        }
        for e in events
    ]
