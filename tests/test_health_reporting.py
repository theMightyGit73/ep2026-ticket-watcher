"""The health check must not contradict itself, and must not chatter.

Two separate lessons, both learned from the running system on 2026-08-16.

`doctor` counted only hard failures when writing its closing line, so it
printed "Everything is working. Nothing to do." directly beneath two WARN
lines — one saying resale was unreadable on 28% of polls, the other saying
the connection was being rate-limited. The summary is the line you actually
read; one that disagrees with the body teaches you to stop reading either.

Separately, the public-IP lookup ran once per watched page rather than once
per cycle, making two identical calls a few seconds apart. Harmless in
itself, but the watcher's whole discipline is about not making requests it
does not need.

And the browser was opened outside the poll loop's error handling, so a
Chrome profile lock still held from the previous instance — which is exactly
what restart.sh produces — killed the process on the way up.

Run with:  .venv/bin/python tests/test_health_reporting.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import __main__ as cli, network  # noqa: E402
from ep_watcher.__main__ import doctor_summary  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


print("\nA clean bill of health is only claimed when there is one")

text, code = doctor_summary([], [])
check_true("says everything is working", "Everything is working" in text)
check("and exits clean", code, 0)

print("\nA warning must reach the summary, not just the body")
# The exact regression: two warnings, nothing broken.

text, code = doctor_summary([], [
    ("Resale visibility", "resale readable on 62/86 polls (72%)"),
    ("Connection", "2 block(s) in the last hour — being rate-limited."),
])
check("never claims everything is working",
      "Everything is working" in text, False)
check_true("says nothing is broken", "Nothing is broken" in text)
check_true("counts them", "2 thing(s)" in text)
check_true("names the resale one", "Resale visibility" in text)
check_true("and carries its detail", "62/86" in text)
check_true("names the connection one", "Connection" in text)
check("but a warning is not a failure exit", code, 0)

print("\nFailures still dominate, and warnings survive alongside them")

text, code = doctor_summary(
    [("Service running", "launchctl load ...")],
    [("Resale visibility", "blind on a third of polls")],
)
check_true("the fix is printed", "launchctl load" in text)
check_true("the warning is not swallowed by the failure",
           "Resale visibility" in text)
check_true("and is introduced as an aside", "Also" in text)
check("a real problem exits non-zero", code, 1)
check("and never claims everything is working",
      "Everything is working" in text, False)

print("\nEvery problem gets a runnable fix beside it")

text, _ = doctor_summary(
    [("Polling", "launchctl kickstart -k gui/501/com.davidcoyne.ep2026watcher"),
     ("Email configured", "edit ~/.ep2026-watcher/env")],
    [],
)
for fix in ("launchctl kickstart", "edit ~/.ep2026-watcher/env"):
    check_true(f"carries {fix[:22]!r}", fix in text)
check_true("and offers the blunt instrument", "restart.sh" in text)

print("\nThe public IP is looked up once a cycle, not once a page")

calls = []


class FakeResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text


def fake_get(url, timeout=None):
    calls.append(url)
    return FakeResponse("86.44.208.194\n")


network.requests = type("_R", (), {"get": staticmethod(fake_get),
                                   "RequestException": Exception})()
network._cache = {"ip": None, "at": 0.0}

first = network.public_ip()
second = network.public_ip()
check("both pages get an answer", (first, second), ("86.44.208.194", "86.44.208.194"))
check("but only one lookup left the machine", len(calls), 1)
check("and it is stripped of whitespace", first, "86.44.208.194")

print("\nA network switch is still noticed on the next cycle")

calls.clear()
fresh = network.public_ip(max_age=0)
check("max_age=0 forces a real lookup", len(calls), 1)

print("\nAn IPv6 answer must never be mistaken for a different connection")
# Measured on 2026-08-17: from the home connection, api.ipify.org answered
# 86.44.208.194 while ifconfig.me and icanhazip.com both answered
# 2001:bb6:4cb5:f000:... A dual-stack connection has both, so which comes
# back depends on the service, not the network. Believing the v6 answer would
# reset the counters, send a switch email for a switch that never happened,
# and — since it is not EP_HOME_IP — label the HOME connection "phone
# hotspot", attributing its blocks to a connection that does not exist.

check("a v6 address is rejected", network._is_ipv4("2001:bb6:4cb5:f000::1"), False)
check("a v4 address is accepted", network._is_ipv4("86.44.208.194"), True)
check("an out-of-range octet is not v4", network._is_ipv4("86.44.208.999"), False)
check("a truncated address is not v4", network._is_ipv4("86.44.208"), False)
check("html or an error page is not v4", network._is_ipv4("<!doctype html>"), False)
check("empty is not v4", network._is_ipv4(""), False)

# Every configured service is the IPv4-pinned form of its hostname.
for url in network._IP_SERVICES:
    check(f"{url} is v4-pinned",
          any(tag in url for tag in ("api4.", "ipv4.", "v4.")), True)

replies = ["2001:bb6:4cb5:f000:81f0:2eb3:1625:7556", "86.44.208.194"]
calls.clear()


def mixed_get(url, timeout=None):
    calls.append(url)
    return FakeResponse(replies[len(calls) - 1] + "\n")


network.requests = type("_R", (), {"get": staticmethod(mixed_get),
                                   "RequestException": Exception})()
network._cache = {"ip": None, "at": 0.0}
check("a v6 reply is skipped for the next service", network.public_ip(), "86.44.208.194")
check("...having tried both", len(calls), 2)

# If every service answers v6, the honest result is "do not know" — which
# leaves the known connection untouched rather than inventing a switch.
network._cache = {"ip": None, "at": 0.0}
calls.clear()
network.requests = type("_R", (), {
    "get": staticmethod(lambda url, timeout=None: (calls.append(url),
                                                   FakeResponse("2001:bb6::1"))[1]),
    "RequestException": Exception})()
check("all-v6 answers give no IP at all", network.public_ip(), None)
check("...rather than a bogus 'new connection'", network._cache["ip"], None)

print("\nAn unreachable IP service does not erase what we knew")
# Returning None makes note_network() skip the poll entirely, so a momentary
# blip must not be read as "the connection changed".

# Seeded here rather than inherited from an earlier section, so this stands
# on its own and cannot be broken by a check inserted above it.
import time as _time  # noqa: E402

network._cache = {"ip": "86.44.208.194", "at": _time.monotonic()}


def exploding_get(url, timeout=None):
    calls.append(url)
    raise network.requests.RequestException("no route to host")


network.requests = type("_R", (), {"get": staticmethod(exploding_get),
                                   "RequestException": Exception})()
calls.clear()
check("the cached answer survives a failure", network.public_ip(), "86.44.208.194")
check("without even trying, while still fresh", len(calls), 0)

check("and a forced lookup that fails returns None",
      network.public_ip(max_age=0), None)
check("having tried every service", len(calls), len(network._IP_SERVICES))
check("but the cache is left intact for the next cycle",
      network._cache["ip"], "86.44.208.194")

print("\nRestarting must not be able to leave nothing running")
# restart.sh kills the old Chrome and launchd starts the new watcher moments
# later. If the profile lock is still held, start() raises — and it used to
# do so outside the poll loop's error handling, killing the process on the
# way up. The one command he is told to run when things break must not have a
# way of leaving nothing behind.

cli.time = type("_NoSleep", (), {"sleep": staticmethod(lambda s: None)})()

starts = []


class LockedThenFree:
    """A BrowserSession that fails to start until `free_after` attempts."""

    free_after = 2
    closed = 0

    def __init__(self):
        starts.append(1)

    def start(self):
        if len(starts) < self.free_after:
            raise RuntimeError("ProcessSingleton: the profile is already in use")
        return self

    def close(self):
        LockedThenFree.closed += 1


cli._browser = lambda: type("_M", (), {"BrowserSession": LockedThenFree})

session = cli._start_session(attempts=3)
check("a held profile lock is retried, not fatal", isinstance(session, LockedThenFree), True)
check("it took the expected number of tries", len(starts), 2)
check("and the failed attempt was cleaned up", LockedThenFree.closed, 1)


class NeverFree(LockedThenFree):
    free_after = 99


starts.clear()
LockedThenFree.closed = 0
cli._browser = lambda: type("_M", (), {"BrowserSession": NeverFree})

raised = None
try:
    cli._start_session(attempts=3)
except Exception as exc:
    raised = exc
check_true("a browser that never starts is still a real failure", raised is not None)
check_true("and says why", "already in use" in str(raised))
check("having tried the full budget", len(starts), 3)
# Exiting hands the problem to launchd, which retries with a clean process.
# Looping here forever would look identical to a working watcher.
check("every attempt was cleaned up", LockedThenFree.closed, 3)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
