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


def public_ip(timeout: float = 6.0) -> Optional[str]:
    """Best-effort public IP. Returns None rather than raising."""
    for url in _IP_SERVICES:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip and len(ip) <= 45:
                    return ip
        except requests.RequestException:
            continue
    return None
