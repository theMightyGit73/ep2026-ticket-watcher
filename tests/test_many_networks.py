"""The watcher knows any number of connections, not two.

It was built when there were two — a home Wi-Fi and a phone hotspot — and the
label logic said so: an address either equalled EP_HOME_IP or it was "the
hotspot". On 2026-08-18 there were three in one morning. A power cut moved the
MacBook onto a tethered eir connection, and then onto a Sky line, and the
second switch was announced as "new address, same connection" — because with
only two names available, both non-home connections were called the same thing
and comparing labels could not tell them apart.

So a connection is now identified by its default gateway's MAC address, which
is the router itself. That is free to read, needs no permission, and is stable
in exactly the case that broke the old scheme: a carrier handing a tether a new
public address every twenty minutes does not change the router. The Wi-Fi SSID
would be the natural identity and cannot be used — macOS redacts it without
Location Services, verified on this Mac.

Naming is optional throughout. An unnamed connection is tracked, counted and
blamed correctly; it is simply described by its address range rather than
named, and the email that announces it says how to name it.

Run with:  .venv/bin/python tests/test_many_networks.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def net(key, ip, gateway="192.168.0.1", subnet="192.168.0.x",
        port="Wi-Fi", hotspot=False):
    return {"key": key, "ip": ip, "gateway": gateway, "gateway_mac": key,
            "subnet": subnet, "port": port, "hotspot": hotspot,
            "interface": "en0"}


HOME = net("9c:31:c3:93:d1:b1", "86.44.208.194")
TETHER = net("aa:bb:cc:00:11:22", "212.129.85.203", gateway="172.20.10.1",
             subnet="172.20.10.x", hotspot=True)
SKY = net("de:ad:be:ef:00:01", "51.186.255.86", gateway="192.168.1.1",
          subnet="192.168.1.x")

saved_names, saved_home = config.NETWORK_NAMES, config.HOME_NETWORK_IP
config.NETWORK_NAMES, config.HOME_NETWORK_IP = {}, None

print("\nThree connections in one morning are three connections")

s = dict(st._defaults())
check("the first is not a change", st.note_network(s, HOME), "")
check("the first is assumed to be home", st.network_label(s), config.HOME_NETWORK_LABEL)

check("moving to the tether is a switch", st.note_network(s, TETHER), "switched")
check("a hotspot names itself", st.network_label(s), config.HOTSPOT_LABEL)

check("moving on again is another switch", st.note_network(s, SKY), "switched")
check("and the third is described, not misnamed",
      st.network_label(s), "the 192.168.1.x network via Wi-Fi")
check_true("which is none of the other two",
           st.network_label(s) not in (config.HOME_NETWORK_LABEL, config.HOTSPOT_LABEL))

check("going back is recognised, not relearned", st.note_network(s, HOME), "switched")
check("and it kept its name", st.network_label(s), config.HOME_NETWORK_LABEL)
check("three connections known", len(s["networks"]), 3)

print("\nA new address on the same router is not a new connection")
# The case the old label comparison could not see. The carrier re-addressed
# this tether three times in fifty minutes on 2026-08-18.

s = dict(st._defaults())
st.note_network(s, TETHER)
again = dict(TETHER, ip="212.129.77.253")
check("same router, new address", st.note_network(s, again), "readdressed")
check("still one connection", len(s["networks"]), 1)
check("and it remembers both addresses",
      s["networks"][TETHER["key"]]["addresses"],
      ["212.129.85.203", "212.129.77.253"])

print("\nBlocks follow the connection, not the address it happened to hold")

s = dict(st._defaults())
st.note_network(s, TETHER)
st.record_block(s, when=st.utc_now() - timedelta(minutes=40))
st.note_network(s, again)          # re-addressed, same router
st.record_block(s, when=st.utc_now() - timedelta(minutes=20))
check("both blocks land on the one connection", s["networks"][TETHER["key"]]["blocks"], 2)
check("and are counted against it", st.recent_blocks(s, 1, ip=TETHER["key"]), 2)

st.note_network(s, SKY)
severity, headline, _ = st.connection_health(s)
check("the new connection is clean", severity, "ok")
check_true("and the burnt one is named as the one in trouble",
           "2 block(s)" in headline and config.HOTSPOT_LABEL in headline)

print("\nNaming is optional, and offered")

check_true("an unnamed connection says so", not st.is_named(s))
check("it offers the key to name it with", st.naming_key(s), SKY["key"])

config.NETWORK_NAMES = {SKY["key"]: "the Sky line"}
check("a configured name wins", st.network_label(s), "the Sky line")
check_true("and it counts as named", st.is_named(s))

# Any identifier David happens to have to hand should work.
config.NETWORK_NAMES = {SKY["gateway"]: "by gateway"}
check("naming by gateway address works", st.network_label(s), "by gateway")
config.NETWORK_NAMES = {SKY["ip"]: "by public address"}
check("naming by public address works", st.network_label(s), "by public address")
config.NETWORK_NAMES = {}

print("\nSwitch advice only ever names somewhere he can actually go")

check("it suggests one of the others", st.other_network_label(s) in
      (config.HOME_NETWORK_LABEL, config.HOTSPOT_LABEL), True)
check_true("and never suggests the one in use",
           st.other_network_label(s) != st.network_label(s))

# The failure this replaced. Sitting on a third network with a day-old tether
# in history, the advice was "move the MacBook to an earlier connection
# (212.129.87.241)" — an address nobody can join, and the cleanest-looking
# candidate precisely because it was dead.
stale = dict(st._defaults())
st.note_network(stale, SKY)
stale["networks"]["212.129.87.241"] = {
    "first_seen": (st.utc_now() - timedelta(hours=20)).isoformat(),
    "searches": 52, "blocks": 0,
}
check_true("a bare address is never offered as a destination",
           "212.129.87.241" not in st.other_network_label(stale))
check("it falls back to the one thing always available",
      st.other_network_label(stale), config.HOTSPOT_LABEL)

# Ranked on recent blocks, not the lifetime tally — which never decays, so a
# connection that misbehaved a week ago would be ruled out forever.
burnt = dict(st._defaults())
st.note_network(burnt, HOME)
st.record_block(burnt, when=st.utc_now() - timedelta(days=4))
st.note_network(burnt, SKY)
check("an old block does not rule a connection out",
      st.other_network_label(burnt), config.HOME_NETWORK_LABEL)
st.note_network(burnt, HOME)   # blocks land on the connection in use
st.record_block(burnt, when=st.utc_now() - timedelta(hours=2))
st.note_network(burnt, SKY)
check("a recent one does", st.other_network_label(burnt), config.HOTSPOT_LABEL)

# And it must not tell him to switch from the hotspot to the hotspot.
on_hotspot = dict(st._defaults())
st.note_network(on_hotspot, SKY)
st.note_network(on_hotspot, TETHER)
check("no advice to switch to where he already is",
      st.other_network_label(on_hotspot), config.HOME_NETWORK_LABEL)
only_hotspot = dict(st._defaults())
st.note_network(only_hotspot, TETHER)
check("with nowhere named to go, it stays honest and vague",
      st.other_network_label(only_hotspot), st.ANY_OTHER_NETWORK)

print("\nThe whole picture, for choosing where to buy")

rows = st.known_networks(s)
# This state has met two: the tether (twice, on two addresses) and the Sky
# line. The re-addressing must not have inflated that.
check("every connection is listed once", len(rows), 2)
check("the one in use comes first", rows[0][4], True)
blocks = {label: n for label, _k, _s, n, _c in rows}
check("and the burnt one is identifiable", blocks[config.HOTSPOT_LABEL], 2)

print("\nUpgrading an old state file is invisible from the inbox")
# State written before connections had an identity is keyed on the public
# address. The same address means the same connection, so adopting the gateway
# as its key must not read as a switch — nobody moved.

s = dict(st._defaults())
st.note_network(s, "86.44.208.194")            # the old, address-keyed world
st.record_block(s, when=st.utc_now() - timedelta(minutes=30))
s.pop("current_net")                            # exactly what an old file has

upgraded = net("9c:31:c3:93:d1:b1", "86.44.208.194")
check("adopting a gateway is not a switch", st.note_network(s, upgraded), "")
check("the connection is now keyed on its router", s["current_net"], upgraded["key"])
check("its history came with it", len(s["networks"]), 1)
check("including what it had been blamed for",
      s["networks"][upgraded["key"]]["blocks"], 1)
check("and the block still counts against it",
      st.recent_blocks(s, 1, ip=upgraded["key"]), 1)

print("\nA bare address still works, for old state and API-only hosts")

s = dict(st._defaults())
check("an IP on its own is accepted", st.note_network(s, "10.0.0.1"), "")
check("and becomes its own identity", s["current_net"], "10.0.0.1")
check("a different one is a switch", st.note_network(s, "10.0.0.2"), "switched")

config.NETWORK_NAMES, config.HOME_NETWORK_IP = saved_names, saved_home

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
