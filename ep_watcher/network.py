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

import re
import time
from typing import Optional

import requests

#: Tried in order. Any of them failing is not an error worth reporting —
#: rotation advice is a convenience, and losing it must never take down a
#: poll that could otherwise have found a ticket.
#:
#: Every one is the IPv4-pinned form of its service, and that is load-bearing
#: rather than tidy. Measured on 2026-08-17 from the home connection, the
#: unpinned hostnames disagreed with each other:
#:
#:   api.ipify.org    -> 86.44.208.194
#:   ifconfig.me/ip   -> 2001:bb6:4cb5:f000:...
#:   icanhazip.com    -> 2001:bb6:4cb5:f000:...
#:
#: A dual-stack connection has both addresses, so which one comes back
#: depends on the service rather than on the network. The watcher treats a
#: different address as a different connection, so a v6 answer would look
#: like a switch that never happened: counters reset, a switch email sent,
#: and — because the v6 address is not EP_HOME_IP — the home connection
#: labelled "phone hotspot". Blocks on home would then be attributed to a
#: connection that does not exist, and the health line would report the
#: connection needed for buying as healthy while it was being throttled.
#: That is the exact misattribution the per-connection accounting exists to
#: prevent, arriving through the front door.
_IP_SERVICES = (
    "https://api4.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://v4.ident.me",
)

#: Shape check, belt and braces over the pinned hostnames above. A service
#: that starts answering with v6 anyway must be ignored rather than believed.
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _is_ipv4(text: str) -> bool:
    if not text or not _IPV4_RE.match(text):
        return False
    return all(int(octet) <= 255 for octet in text.split("."))


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
                if _is_ipv4(ip):
                    _cache.update(ip=ip, at=now)
                    return ip
        except requests.RequestException:
            continue
    # Deliberately does not clear the cache, and deliberately returns None
    # rather than whatever last came back. A momentary failure to reach an
    # IP-echo service is not evidence the connection changed, and None makes
    # note_network() leave the known connection alone — far better than
    # inventing a switch that never happened.
    return None
