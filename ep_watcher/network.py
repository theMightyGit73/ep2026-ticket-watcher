"""Which connection is the watcher currently going out through?

The watcher alternates between David's home Wi-Fi and his phone hotspot, by
hand. The point is to split the request volume across two connections so that
neither accumulates enough to get rate-limited — and so that if one does get
flagged, the other is still available to actually buy a ticket with.

Detecting the public IP is what makes that workable rather than annoying: the
watcher can *tell* when he has switched, so it stops asking, resets its
counters, and starts the clock on the new connection. No confirmation step,
no button to press.

This talks to a plain IP-echo service, not to Ticketmaster, so it costs
nothing against the rate limit that matters.
"""

import time
from typing import Optional

import requests

#: Tried in order. Any of them failing is not an error worth reporting —
#: rotation advice is a convenience, and losing it must never take down a
#: poll that could otherwise have found a ticket.
_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


#: How long a looked-up IP stays good for. Every watched page is polled on
#: every cycle and each one asks which connection it went out through, so
#: without this a two-page cycle makes two identical lookups a few seconds
#: apart. Well under the poll interval, so a real network switch is still
#: noticed on the very next cycle.
CACHE_SECONDS = 120.0

_cache = {"ip": None, "at": 0.0}


def public_ip(timeout: float = 6.0, max_age: float = CACHE_SECONDS) -> Optional[str]:
    """Best-effort public IP. Returns None rather than raising.

    Pass max_age=0 to force a fresh lookup.
    """
    now = time.monotonic()
    if _cache["ip"] and (now - _cache["at"]) < max_age:
        return _cache["ip"]

    for url in _IP_SERVICES:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip and len(ip) <= 45:
                    _cache.update(ip=ip, at=now)
                    return ip
        except requests.RequestException:
            continue
    # Deliberately does not clear the cache. A momentary failure to reach an
    # IP-echo service is not evidence the connection changed, and returning
    # None makes note_network() skip the poll entirely.
    return None
