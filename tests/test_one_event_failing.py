"""One page breaking while the other is fine must not go unnoticed.

This is a regression test for a bug that existed for exactly one day, and it
is worth stating plainly because it is the exact failure this project was
built to eliminate.

consecutive_failures was a single global counter. Adding a second ticket page
meant a healthy page reset the count that a broken page had just incremented,
so the counter never got past 1. The instalment page could have failed on
every single cycle for a fortnight: the watchdog would never fire, and the
hourly email would report everything fine — silence that looks exactly like
"no tickets yet".

The likeliest real cause is mundane. Ticketmaster changes a URL, one page
starts 404ing, and half the coverage disappears with no symptom at all.

Run with:  .venv/bin/python tests/test_one_event_failing.py
"""

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import UNAVAILABLE, Reading  # noqa: E402

failures = []
alerts = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


notify._send_email = lambda *a, **kw: None
notify._send_ntfy = lambda *a, **kw: None
notify.watchdog = lambda reason, failures_, **kw: alerts.append((reason, failures_))
engine.network = type("_IP", (), {"public_ip": staticmethod(lambda *a, **kw: "10.0.0.1")})()
engine.liveness = type("_L", (), {"publish": staticmethod(lambda *a, **kw: True)})()

A, B = config.EVENTS[0], config.EVENTS[1]


def healthy(event):
    return Reading(source="stub", event_slug=event.slug, event_name=event.name,
                   event_url=event.url, primary=UNAVAILABLE, resale=UNAVAILABLE)


def broken(event):
    r = Reading(source="browser", event_slug=event.slug,
                event_name=event.name, event_url=event.url)
    r.failed = True
    return r


def simulate(cycles=30):
    alerts.clear()
    state = dict(st._defaults())
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(cycles):
            engine.handle(healthy(A), state)
            engine.handle(broken(B), state)
    return state


print("\nA healthy page must not reset the broken page's failure count")

state = simulate()
check("the working page reads zero", st.event_state(state, A.slug)["consecutive_failures"], 0)
check("the broken page counts every failure",
      st.event_state(state, B.slug)["consecutive_failures"], 30)
check("the global counter follows the worst", state["consecutive_failures"], 30)

print("\nAnd the watchdog must actually fire")

check_true("it alerted", len(alerts) > 0)
check_true("at or near the threshold, not after 30 cycles",
           alerts[0][1] <= config.WATCHDOG_FAILURE_THRESHOLD + 1)

print("\nThe alert must say WHICH page, or you are left guessing")

reason = alerts[0][0]
check_true("names the failing page", B.name in reason)
check_true("says how badly", "failed checks in a row" in reason)
check_true("and says what still works", A.name in reason)

print("\nThe reverse case behaves the same")

alerts.clear()
state = dict(st._defaults())
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(10):
        engine.handle(broken(A), state)
        engine.handle(healthy(B), state)
check("failures attributed to A", st.event_state(state, A.slug)["consecutive_failures"], 10)
check("B stays clean", st.event_state(state, B.slug)["consecutive_failures"], 0)
check_true("still alerts", len(alerts) > 0)
check_true("naming A this time", A.name in alerts[0][0])

print("\nBoth pages healthy means silence")

alerts.clear()
state = dict(st._defaults())
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(20):
        engine.handle(healthy(A), state)
        engine.handle(healthy(B), state)
check("no failures recorded", state["consecutive_failures"], 0)
check("and no watchdog noise", len(alerts), 0)

print("\nA page recovering clears only its own count")

state = simulate(cycles=10)
with contextlib.redirect_stdout(io.StringIO()):
    engine.handle(healthy(B), state)
check("the recovered page resets", st.event_state(state, B.slug)["consecutive_failures"], 0)
check("and the global count follows it down", state["consecutive_failures"], 0)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
