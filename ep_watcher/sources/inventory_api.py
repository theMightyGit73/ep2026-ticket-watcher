"""Source: Ticketmaster's Inventory Status API.

This is the endpoint Ticketmaster built to answer precisely the question we
are asking, for precisely this market. It reports primary and resale status
separately, near-real-time, and Ireland is a supported region. No bot
detection, no login, no session to expire — it is strictly better than
scraping in every dimension except one: it needs an access grant.

Access is not part of the free Discovery signup. You request it by mailing
devportalinquiry@ticketmaster.com — see the README. Until a key lands in
TM_API_KEY this source reports itself unconfigured and the browser source
carries the watch on its own.

  https://developer.ticketmaster.com/products-and-docs/apis/inventory-status/
"""

import requests

from .. import config
from ..model import AVAILABLE, FEW_LEFT, UNAVAILABLE, UNKNOWN, Reading

SOURCE = "inventory-status-api"

#: Ticketmaster's vocabulary → ours.
_STATUS_MAP = {
    "TICKETS_AVAILABLE": AVAILABLE,
    "FEW_TICKETS_LEFT": FEW_LEFT,
    "TICKETS_NOT_AVAILABLE": UNAVAILABLE,
    "UNKNOWN": UNKNOWN,
}


def configured() -> bool:
    return bool(config.TM_API_KEY)


def check() -> Reading:
    reading = Reading(source=SOURCE)

    if not configured():
        reading.failed = True
        return reading.note("TM_API_KEY not set — source skipped")

    try:
        resp = requests.get(
            config.INVENTORY_API_URL,
            params={"events": config.TM_EVENT_ID, "apikey": config.TM_API_KEY},
            timeout=20,
        )
    except requests.RequestException as exc:
        reading.failed = True
        return reading.note(f"request failed: {exc}")

    if resp.status_code in (401, 403):
        reading.failed = True
        return reading.note(
            f"HTTP {resp.status_code} — key not authorised for inventory-status. "
            "This API needs an explicit grant, not just a Discovery key."
        )
    if resp.status_code != 200:
        reading.failed = True
        return reading.note(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError:
        reading.failed = True
        return reading.note(f"non-JSON response: {resp.text[:200]}")

    # Documented as an array, one entry per requested event id.
    entries = payload if isinstance(payload, list) else [payload]
    entry = next(
        (e for e in entries if str(e.get("eventId", "")) == str(config.TM_EVENT_ID)),
        entries[0] if entries else None,
    )
    if entry is None:
        reading.failed = True
        return reading.note("empty response — is TM_EVENT_ID a valid universal id?")

    raw_primary = entry.get("status", "UNKNOWN")
    raw_resale = entry.get("resaleStatus", "UNKNOWN")
    reading.primary = _STATUS_MAP.get(raw_primary, UNKNOWN)
    reading.resale = _STATUS_MAP.get(raw_resale, UNKNOWN)
    reading.note(f"api says status={raw_primary} resaleStatus={raw_resale}")

    # An UNKNOWN here usually means the id or the market is wrong rather than
    # that stock is genuinely indeterminate, so surface why — otherwise this
    # source fails silently and looks like a clean "no tickets" forever.
    for label, key in (("status", "statusDetail"), ("resale", "resaleStatusDetail")):
        if entry.get(key):
            reading.note(f"{label} detail: {entry[key]}")
    if reading.primary == UNKNOWN and reading.resale == UNKNOWN:
        reading.failed = True
        reading.note("both statuses UNKNOWN — treating as a failed read, not as 'no tickets'")

    return reading


# ── Setup helper ─────────────────────────────────────────────────────────────

def resolve_event_id(api_key: str) -> list:
    """Find the Discovery 'universal' event id for the event.

    The id in the ticketmaster.ie URL (18006314BD813D3E) is a host/legacy id.
    Inventory Status wants the universal one, and they are not always the same
    string, so guessing is how you end up staring at INVALID_EVENT_ID.
    """
    resp = requests.get(
        config.DISCOVERY_API_URL,
        params={
            "apikey": api_key,
            "keyword": "Electric Picnic",
            "countryCode": "IE",
            "size": 50,
        },
        timeout=20,
    )
    resp.raise_for_status()
    events = resp.json().get("_embedded", {}).get("events", [])
    return [
        {
            "id": e.get("id"),
            "name": e.get("name"),
            "date": e.get("dates", {}).get("start", {}).get("localDate"),
            "status": e.get("dates", {}).get("status", {}).get("code"),
            "url": e.get("url"),
        }
        for e in events
    ]
