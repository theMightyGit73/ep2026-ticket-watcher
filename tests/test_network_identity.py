"""Recognising one connection from another, without touching the network.

This module decides which connection a poll went out through, and everything
downstream depends on it being right: which network gets the blame for a
block, when the rotation nudge fires, and what the "connection changed" email
says. Getting it wrong is not cosmetic — on 2026-08-18 a move from the eir
hotspot onto a Sky line was announced as "new address, same connection",
because the comparison was on labels rather than on the router.

None of these checks shell out. They exercise the parsing and the judgement,
which is the part that can be wrong in a way nobody notices.

Run with:  .venv/bin/python tests/test_network_identity.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import network  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


print("\nA router's MAC is one router, however macOS prints it")
# macOS prints single-digit octets unpadded. Unpadded and padded forms of the
# SAME router must not be recorded as two different networks, or the block
# history splits in half and neither half looks bad enough to act on.
calls = []


def fake_arp(out):
    def _run(*args, timeout=4.0):
        calls.append(args)
        return out
    return _run


real_run = network._run
try:
    network._run = fake_arp("? (192.168.1.254) at 64:fd:96:d0:d2:37 on en0 ifscope [ethernet]")
    check("a fully-padded MAC reads back as itself",
          network.gateway_mac("192.168.1.254"), "64:fd:96:d0:d2:37")

    network._run = fake_arp("? (192.168.0.1) at 9:1c:c3:3:d1:b1 on en0 ifscope [ethernet]")
    check("and an unpadded one is normalised to match",
          network.gateway_mac("192.168.0.1"), "09:1c:c3:03:d1:b1")

    network._run = fake_arp("? (10.0.0.1) at 9C:31:C3:93:D1:B1 on en0 [ethernet]")
    check("case is normalised too", network.gateway_mac("10.0.0.1"), "9c:31:c3:93:d1:b1")

    network._run = fake_arp("? (10.0.0.1) -- no entry")
    check("an ARP miss is empty, never a guess", network.gateway_mac("10.0.0.1"), "")

    network._run = fake_arp("")
    check("and a failed command is empty too", network.gateway_mac("10.0.0.1"), "")

    calls.clear()
    check("no gateway means no lookup at all", network.gateway_mac(""), "")
    check("and nothing was run", len(calls), 0)

    print("\nThe default route is read from macOS's own output")
    network._run = fake_arp(
        "   route to: default\n"
        "destination: default\n"
        "       mask: default\n"
        "    gateway: 192.168.1.254\n"
        "  interface: en0\n"
        "      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>\n"
    )
    check("gateway and interface", network.default_route(), ("192.168.1.254", "en0"))

    network._run = fake_arp("   route to: default\n   (no route)\n")
    check("no route is two empty strings, not a crash",
          network.default_route(), ("", ""))
finally:
    network._run = real_run


print("\nA phone hotspot is recognised, and only suggested")
# Apple hands out 172.20.10.x on Personal Hotspot. This only ever SUGGESTS a
# name for a connection nobody has named, so a false positive costs a label
# David can change — but a false negative means the hotspot inherits the home
# connection's block history, which is the reading that matters.
check("Apple's hotspot gateway", network.looks_like_hotspot("172.20.10.1", "Wi-Fi"), True)
check("a tethered iPhone by port name",
      network.looks_like_hotspot("192.168.1.254", "iPhone USB"), True)
check("case-insensitively", network.looks_like_hotspot("10.0.0.1", "iphone usb"), True)
check("an ordinary router is not a hotspot",
      network.looks_like_hotspot("192.168.1.254", "Wi-Fi"), False)
check("nor is the 192.168.0.x line seen on 2026-08-18",
      network.looks_like_hotspot("192.168.0.1", "Wi-Fi"), False)
check("missing port is handled", network.looks_like_hotspot("192.168.1.1", None), False)
check("missing everything is handled", network.looks_like_hotspot("", ""), False)


print("\nOnly an IPv4 address counts as the public address")
# A dual-stack connection reports both, and recording the v6 address as well
# made one connection look like two — each with half the history. Fixed on
# 2026-08-17; this pins it.
check("a v4 address", network._is_ipv4("86.44.208.194"), True)
check("another", network._is_ipv4("51.171.188.125"), True)
check("a v6 address is not v4",
      network._is_ipv4("2a01:b340:63:76c7:3d2e:966a:46ae:cc8d"), False)
check("nor is a hostname", network._is_ipv4("example.com"), False)
check("nor is empty", network._is_ipv4(""), False)
check("nor is nonsense with the right shape", network._is_ipv4("999.1.1.1"), False)
check("nor a truncated address", network._is_ipv4("86.44.208"), False)

# _is_ipv4 is deliberately strict about whitespace, and the stripping is the
# caller's job. Worth pinning as a pair: an IP-echo service returns its answer
# with a trailing newline, so if public_ip ever stopped stripping, every
# lookup would be rejected as malformed and the watcher would silently lose
# track of which connection it is on.
check("padding is rejected by the validator", network._is_ipv4(" 86.44.208.194 "), False)

real_get = network.requests.get


class FakeResponse:
    status_code = 200
    text = "86.44.208.194\n"


try:
    network.requests.get = lambda url, timeout=None: FakeResponse()
    network._cache.update(ip=None, at=0.0)
    check("and the caller strips before validating",
          network.public_ip(max_age=0), "86.44.208.194")
finally:
    network.requests.get = real_get
    network._cache.update(ip=None, at=0.0)


print("\nHardware ports are read once and cached")
# The slowest call in the module, and it changes only when hardware is
# plugged in. It being cached is deliberate; it being cached WRONG would
# mislabel every connection for the life of the process.
network._PORTS.clear()
real_run = network._run
runs = []
try:
    def counting_run(*args, timeout=4.0):
        runs.append(args)
        return ("(Hardware Port: Wi-Fi, Device: en0)\n"
                "(Hardware Port: iPhone USB, Device: en5)\n")
    network._run = counting_run
    first = network.hardware_ports()
    check("Wi-Fi is mapped from its device", first.get("en0"), "Wi-Fi")
    check("and so is a tethered iPhone", first.get("en5"), "iPhone USB")
    second = network.hardware_ports()
    check("a second call does not shell out again", len(runs), 1)
    check("and returns the same mapping", second, first)
finally:
    network._run = real_run
    network._PORTS.clear()

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
