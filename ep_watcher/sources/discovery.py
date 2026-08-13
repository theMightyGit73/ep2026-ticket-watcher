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

import time
from typing import List, Optional

import requests

from .. import config
from ..model import AVAILABLE, UNAVAILABLE, Listing, Reading

SOURCE = "discovery-api"

#: Discovery allows 5 requests per second and enforces it with a spike-arrest
#: that returns HTTP 429. Two calls back to back tripped it during testing,
#: so they are spaced out.
_RATE_LIMIT_PAUSE = 1.0


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
    """Watch for the event *reappearing* in the Discovery index.

    Measured on 2026-08-13: the Weekend Camping event is not in Discovery at
    all. Five independent angles agree — keyword search, venue search, every
    IE event across the festival weekend, the Electric Picnic attraction
    record (which reports exactly two upcoming events), and the suggest
    endpoint. Only the two campervan passes are listed.

    The natural reading is that Discovery indexes purchasable inventory, and
    a sold-out event drops out of it. That turns absence into the signal:
    polling this event's status is impossible, but noticing it *come back*
    is easy, and its return would mean stock went live.

    What this cannot do is see a single Verified Resale listing appearing for
    five minutes. It is a coarse net for a genuine re-release, running free
    and with no risk to David's IP or account — not a substitute for the
    browser.
    """
    reading = Reading(source=SOURCE)

    if not configured():
        reading.failed = True
        return reading.note("TM_DISCOVERY_KEY not set — source skipped")

    try:
        events = search_events()
        time.sleep(_RATE_LIMIT_PAUSE)
        resale = find_resale_events()
    except PermissionError as exc:
        reading.failed = True
        return reading.note(str(exc))
    except requests.RequestException as exc:
        reading.failed = True
        return reading.note(f"request failed: {exc}")

    if not events:
        reading.failed = True
        return reading.note("Discovery returned no Electric Picnic events at all — unexpected")

    named = [e for e in events if _is_wanted_event(e)]
    reading.note(f"{len(events)} Electric Picnic event(s) indexed; {len(named)} match the wanted ticket")

    if named:
        reading.primary = AVAILABLE
        for e in named:
            reading.listings.append(
                Listing(name=f"{e['name']} [{e.get('status')}]", price=e.get("price"), kind="primary")
            )
        reading.note(
            "THE EVENT IS BACK IN THE DISCOVERY INDEX — it was absent while sold out, "
            "so this means inventory went live"
        )
    else:
        reading.primary = UNAVAILABLE
        reading.note(
            "wanted event still absent from the index (expected while sold out); "
            f"indexed: {', '.join(e['name'] for e in events[:4])}"
        )

    if resale:
        reading.resale = AVAILABLE
        for e in resale:
            reading.listings.append(
                Listing(name=f"TMR resale: {e['name']}", price=e.get("price"), kind="resale")
            )
        reading.note(f"{len(resale)} tmr-sourced event(s)")
    else:
        reading.resale = UNAVAILABLE
        reading.note("no tmr-sourced events")

    return reading


def _is_wanted_event(event: dict) -> bool:
    """Does this indexed event look like the ticket David actually wants?

    Matched on name rather than id because the id in the ticketmaster.ie URL
    (18006314BD813D3E) is a host id that Discovery does not recognise — a
    direct lookup by it returns 404, confirmed.
    """
    name = (event.get("name") or "").lower()
    if not all(word in name for word in config.DISCOVERY_MATCH_WORDS):
        return False
    # The campervan passes are permanently indexed and are not the wanted
    # ticket; without this they would look like a permanent "available".
    return not any(word in name for word in config.DISCOVERY_EXCLUDE_WORDS)


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
            "price": _first_price(e),
            "url": e.get("url"),
        }
        for e in events
    ]
