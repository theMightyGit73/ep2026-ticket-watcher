"""An outage must announce its own end, and must not outlive itself.

Reconstructed from the blackout of 2026-08-18. The Mac lost its connection at
15:53 and regained it at 17:04. Every watchdog alert during it failed to send,
because the fault WAS the network — which is correct behaviour, and which
makes the recovery notice the only word David would ever get about it. He got
nothing. Worse, `outage_started_at` was left set, so it was still pointing at
15:53 the following afternoon; the next outage's recovery email would have
reported a blackout measured from the previous day.

The cause was subtle and worth pinning. The gate read the GLOBAL failure
counter at the moment each page recovered. Pages have different intervals —
the standard page is searched about five times as often as the instalment plan
— so during that outage the standard page reached 11 failures while the
instalment plan reached 3. When the standard page recovered first, the global
counter (the worst surviving page) fell to 3. By the time the instalment plan
recovered and the "everything is healthy" condition was finally satisfied, the
counter it was compared against was 3, below the threshold of 4, and the
branch never ran.

Run with:  .venv/bin/python tests/test_outage_recovery.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, engine, notify, state as st  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Reading  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


recovered_calls = []
notify.recovered = lambda after, mins=0.0, reason="": recovered_calls.append(
    {"after": after, "minutes": mins, "reason": reason}
) or True
notify.available = lambda *a, **k: True
notify.watchdog = lambda *a, **k: True
notify.heartbeat = lambda *a, **k: True

BUSY, QUIET = config.EVENTS[0], config.EVENTS[1]


def reading_for(event, ok=True):
    """A reading for one page — either a clean answer or a total failure."""
    r = Reading(source="browser")
    r.event_slug, r.event_name, r.event_url = event.slug, event.name, event.url
    if ok:
        r.primary, r.resale = UNAVAILABLE, UNAVAILABLE
    else:
        r.failed = True
    return r


def fresh():
    s = dict(st._defaults())
    s["last_success"] = st.utc_now().isoformat()
    return s


print("\nThe 2026-08-18 blackout, replayed")
# The busy page is searched ~5x as often, so it fails ~5x as many times.
s = fresh()
recovered_calls.clear()
for _ in range(11):
    engine.handle(reading_for(BUSY, ok=False), s)
for _ in range(3):
    engine.handle(reading_for(QUIET, ok=False), s)

check("the outage is recorded as started", bool(s["outage_started_at"]), True)
check("the peak is the worst page's streak", s.get("outage_peak_failures"), 11)
check("the global counter is the worst survivor", s["consecutive_failures"], 11)

# The busy page recovers first. The outage is NOT over — the quiet page is
# still broken — so nothing should be announced or cleared yet.
engine.handle(reading_for(BUSY, ok=True), s)
check("no notice while a page is still broken", len(recovered_calls), 0)
check_true("and the outage is still recorded", s["outage_started_at"])
check("the global counter falls to the survivor", s["consecutive_failures"], 3)

# The quiet page recovers. THIS is the moment the old gate missed: was_broken
# is now 3, below the threshold of 4, so the branch never ran.
engine.handle(reading_for(QUIET, ok=True), s)
check("recovery is announced once", len(recovered_calls), 1)
check("and reports the outage's real peak, not the survivor's",
      recovered_calls[0]["after"], 11)
check("the outage clock is cleared", s["outage_started_at"], None)
check("the peak is cleared", s.get("outage_peak_failures"), 0)
check("the failure reason is cleared", s["last_failure_reason"], None)
check("the watchdog clock is cleared", s["last_watchdog_alert"], None)

print("\nRecovery is announced exactly once, not once per page")
s = fresh()
recovered_calls.clear()
for _ in range(6):
    engine.handle(reading_for(BUSY, ok=False), s)
    engine.handle(reading_for(QUIET, ok=False), s)
engine.handle(reading_for(BUSY, ok=True), s)
engine.handle(reading_for(QUIET, ok=True), s)
check("one notice for one recovery", len(recovered_calls), 1)
# And a further healthy poll must not re-announce it.
engine.handle(reading_for(BUSY, ok=True), s)
engine.handle(reading_for(QUIET, ok=True), s)
check("and it does not repeat on later healthy polls", len(recovered_calls), 1)

print("\nA blip too small to announce still cleans up after itself")
# Below the watchdog threshold: no email is warranted, because David was never
# told anything was wrong. But the bookkeeping must still be cleared, or it
# poisons the duration reported by the NEXT outage — which is exactly what
# happened on 2026-08-18, leaving a 21-hour-stale timestamp behind.
s = fresh()
recovered_calls.clear()
engine.handle(reading_for(BUSY, ok=False), s)
check_true("a single failure still starts the clock", s["outage_started_at"])
engine.handle(reading_for(BUSY, ok=True), s)
check("too small to be worth an email", len(recovered_calls), 0)
check("but the clock is cleared anyway", s["outage_started_at"], None)
check("and so is the reason", s["last_failure_reason"], None)

print("\nA stale clock cannot inflate the next outage's duration")
# The failure mode this protects against, stated directly: a leftover
# timestamp from yesterday makes today's two-minute blip report as a
# day-long blackout.
s = fresh()
recovered_calls.clear()
engine.handle(reading_for(BUSY, ok=False), s)
engine.handle(reading_for(BUSY, ok=True), s)
first_clock = s["outage_started_at"]
check("cleared after the first blip", first_clock, None)

for _ in range(5):
    engine.handle(reading_for(BUSY, ok=False), s)
    engine.handle(reading_for(QUIET, ok=False), s)
engine.handle(reading_for(BUSY, ok=True), s)
engine.handle(reading_for(QUIET, ok=True), s)
check("the second outage is announced", len(recovered_calls), 1)
check_true("and its duration is measured from its own start, not yesterday's",
           recovered_calls[0]["minutes"] < 60)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
