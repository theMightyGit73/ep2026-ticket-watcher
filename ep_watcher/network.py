"""Which connection is the watcher currently going out through?

The watcher moves between connections — a home Wi-Fi, David's phone hotspot,
and whatever else the MacBook is joined to on a given day. The point is to
split request volume so no one connection accumulates enough to be
rate-limited, and so that if one does get flagged there is still a healthy one
to actually buy a ticket with.

Identifying the connection is what makes that workable rather than annoying:
the watcher can *tell* when it has moved, so it stops asking, resets its
counters, and attributes blocks to the connection they happened on.

## Why not the SSID

The obvious identity is the Wi-Fi network's name, and it is not available.
Measured on this Mac, 2026-08-18:

    networksetup -getairportnetwork en0  ->  "You are not associated with an
                                              AirPort network."  (while associated)
    ipconfig getsummary en0 | grep SSID  ->  "SSID : <redacted>"

macOS withholds the SSID from any process without Location Services
permission. That is a GUI grant, it does not survive being run under launchd
reliably, and a watcher that silently loses the ability to tell two networks
apart is worse than one that never had it.

## What is used instead

The default gateway's MAC address, read out of the ARP table. It is the router
itself, so it is:

  * free — no permission, no prompt, two cheap subprocess calls;
  * stable — a carrier handing the tether a new public address every twenty
    minutes does not change the router you are talking to, which is exactly
    the case that made the old IP-keyed identity report a switch that never
    happened;
  * unique — a different network means a different router means a different
    MAC, however many networks there turn out to be.

The public IP is still recorded, because it is what Ticketmaster actually
sees and it is worth showing. It is simply no longer the identity.

Everything here is best-effort and returns partial answers rather than
raising: knowing which connection is in use is a convenience, and losing it
must never take down a poll that could have found a ticket.
"""

import re
import subprocess
import time
from typing import Optional

import requests


def _run(*args, timeout: float = 4.0) -> str:
    """Run a command, returning "" on any failure at all."""
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_ROUTE_RE = re.compile(r"^\s*(gateway|interface):\s*(\S+)", re.M)


def default_route() -> tuple:
    """(gateway_ip, interface) for the connection traffic is leaving by."""
    found = dict(_ROUTE_RE.findall(_run("route", "-n", "get", "default")))
    return found.get("gateway", ""), found.get("interface", "")


_ARP_RE = re.compile(r"at\s+([0-9a-f:]{11,17})\s", re.I)


def gateway_mac(gateway: str) -> str:
    """The router's own MAC address, normalised, or "" if it cannot be read.

    macOS prints single-digit octets unpadded — "9c:31:c3:93:d1:b1" but also
    "0:1c:..." — so the octets are zero-padded here to keep one router from
    being recorded as two different networks.
    """
    if not gateway:
        return ""
    match = _ARP_RE.search(_run("arp", "-n", gateway))
    if not match:
        return ""
    return ":".join(part.zfill(2) for part in match.group(1).lower().split(":"))


#: device -> the human name macOS gives that port ("Wi-Fi", "iPhone USB").
#: Read once per process: it changes only when hardware is plugged in, and the
#: lookup is the slowest call here.
_PORTS = {}


def hardware_ports() -> dict:
    if _PORTS:
        return _PORTS
    text = _run("networksetup", "-listnetworkserviceorder", timeout=8.0)
    for port, device in re.findall(
        r"\(Hardware Port:\s*([^,]+),\s*Device:\s*([^)]+)\)", text
    ):
        _PORTS[device.strip()] = port.strip()
    return _PORTS


#: Apple hands out 172.20.10.x on Personal Hotspot, and has done for years.
#: A useful hint, never a certainty — it is only ever used to *suggest* a name
#: for a connection nobody has named, and David can rename it.
_HOTSPOT_GATEWAYS = ("172.20.10.1",)


def looks_like_hotspot(gateway: str, port: str) -> bool:
    if gateway in _HOTSPOT_GATEWAYS:
        return True
    return "iphone" in (port or "").lower()


def fingerprint(max_age: float = 120.0) -> dict:
    """Everything cheap that identifies the connection in use.

    Returns a dict that is always safe to read: every value may be "" or None,
    and `key` falls back through gateway MAC -> interface+gateway -> public IP
    -> "" so that something usable survives even on a machine where none of
    these commands exist.
    """
    now = time.monotonic()
    if _fp_cache["at"] and (now - _fp_cache["at"]) < max_age:
        return dict(_fp_cache["fp"])

    gateway, interface = default_route()
    mac = gateway_mac(gateway)
    port = hardware_ports().get(interface, "")
    ip = public_ip(max_age=max_age)

    key = mac or (f"{interface}:{gateway}" if gateway else "") or ip or ""
    fp = {
        "key": key,
        "gateway": gateway,
        "gateway_mac": mac,
        "interface": interface,
        "port": port,
        "ip": ip,
        "hotspot": looks_like_hotspot(gateway, port),
        # The private range, which is the part a human recognises when nothing
        # has a name yet: "the 192.168.0.x network" means something to someone
        # who has seen their router's admin page.
        "subnet": ".".join(gateway.split(".")[:3]) + ".x" if gateway.count(".") == 3 else "",
    }
    _fp_cache.update(fp=fp, at=now)
    return dict(fp)


_fp_cache = {"fp": {}, "at": 0.0}


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
