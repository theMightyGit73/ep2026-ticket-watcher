"""Check that a wedged watcher gets noticed and restarted.

launchd restarts the watcher when it crashes. Nothing restarts it when it
merely hangs — a stuck Chrome keeps its PID, satisfies every check launchd
makes, and does nothing at all. The only symptom is silence, which this
project refuses to leave ambiguous.

The liveness signal is last_check_at, written on every poll whether it
succeeded or failed. These checks cover the staleness maths and the
watchdog's decision, using its dry-run mode so a passing test never bounces
a watcher that is working.

Run with:  .venv/bin/python tests/test_liveness.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ep_watcher import config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


print("\nMeasuring how long since the watcher did anything")

s = dict(st._defaults())
check("a watcher that has never polled", st.hours_since_check(s), None)

s["last_check_at"] = st.utc_now().isoformat()
age = st.hours_since_check(s)
check("a fresh poll reads as ~0h", age is not None and age < 0.05, True)

s["last_check_at"] = (st.utc_now() - timedelta(hours=2)).isoformat()
age = st.hours_since_check(s)
check("a two-hour-old poll", round(age), 2)

s["last_check_at"] = "not a timestamp"
check("garbage does not crash it", st.hours_since_check(s), None)


def run_watchdog(state_dict, stale_minutes=45):
    """Run watchdog.sh against a fixture state, in dry-run."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(state_dict, f)
        path = f.name
    log = path + ".log"
    try:
        env = dict(os.environ)
        env.update({
            # See test_hold_not_restarted.py: watchdog.sh correctly refuses
            # to restart anything past the stop date, so every check here that
            # expects a restart began failing on the calendar once the event
            # passed. Pinned forward — the logic under test is liveness, not
            # the date.
            "EP_STOP_AFTER": "2099-12-31",
            "EP_STATE_FILE": path,
            "EP_WATCHDOG_DRY_RUN": "1",
            "EP_STALE_MINUTES": str(stale_minutes),
            # Never the real one. Running the tests used to write "restarting
            # the watcher" into the operational log, which is exactly the file
            # you read when something has actually gone wrong.
            "EP_WATCHDOG_LOG": log,
        })
        result = subprocess.run(
            ["bash", str(REPO / "watchdog.sh")],
            capture_output=True, text=True, env=env, timeout=60,
        )
        return "WOULD_RESTART" in result.stdout
    finally:
        os.unlink(path)
        if os.path.exists(log):
            os.unlink(log)


print("\nThe watchdog's decision")

now = st.utc_now().isoformat()
check("leaves a healthy watcher alone", run_watchdog({"last_check_at": now}), False)

recent = (st.utc_now() - timedelta(minutes=20)).isoformat()
check("tolerates a normal gap between polls",
      run_watchdog({"last_check_at": recent}), False)

stale = (st.utc_now() - timedelta(minutes=90)).isoformat()
check("restarts a wedged watcher", run_watchdog({"last_check_at": stale}), True)

edge = (st.utc_now() - timedelta(minutes=44)).isoformat()
check("does not fire just under the limit", run_watchdog({"last_check_at": edge}), False)

edge = (st.utc_now() - timedelta(minutes=46)).isoformat()
check("fires just over it", run_watchdog({"last_check_at": edge}), True)

print("\nIt must not 'repair' things that are not broken")

check("no timestamp yet — watcher may be starting up",
      run_watchdog({}), False)
check("unreadable timestamp is not treated as stale",
      run_watchdog({"last_check_at": "nonsense"}), False)

print("\nA deliberate 403 backoff is not a hang, and must not be restarted")
# The two look identical from outside — in both cases last_check_at stops
# advancing — but the right response is opposite. The backoff doubles to a
# three-hour cap, so past 45 minutes the watchdog would restart a watcher
# that is resting on purpose. Each restart polls the rate-limited connection
# again immediately, unattended, turning a short block into a long one.

stale = (st.utc_now() - timedelta(minutes=90)).isoformat()
resting = (st.utc_now() + timedelta(minutes=40)).isoformat()

check("a stale clock alone still restarts",
      run_watchdog({"last_check_at": stale}), True)
check("...but not while a backoff is still running",
      run_watchdog({"last_check_at": stale, "backoff_until": resting}), False)

expired = (st.utc_now() - timedelta(minutes=5)).isoformat()
check("once the backoff has expired it is a hang again",
      run_watchdog({"last_check_at": stale, "backoff_until": expired}), True)
check("a garbled backoff marker never blocks a needed restart",
      run_watchdog({"last_check_at": stale, "backoff_until": "nonsense"}), True)
check("nor does a null one",
      run_watchdog({"last_check_at": stale, "backoff_until": None}), True)

print("\nLateness is judged against the cadence in force, not a fixed number")
# The flat 45-minute limit only ever matched the daytime cycle. Overnight the
# interval is 30 minutes jittered to 37.5, and the gap includes the poll
# itself — a real 38-minute gap was seen on 2026-08-17, seven minutes short of
# restarting a healthy watcher. The watcher now says when it will be back.

soon = (st.utc_now() + timedelta(minutes=25)).isoformat()
check("a watcher sleeping out a long overnight interval is left alone",
      run_watchdog({"last_check_at": stale, "next_poll_due": soon}), False)

just_due = (st.utc_now() - timedelta(minutes=5)).isoformat()
check("...and is still given time to actually run the poll",
      run_watchdog({"last_check_at": stale, "next_poll_due": just_due}), False)

well_overdue = (st.utc_now() - timedelta(minutes=25)).isoformat()
check("but a poll long overdue is a hang",
      run_watchdog({"last_check_at": stale, "next_poll_due": well_overdue}), True)

# By day this is stricter than the old flat limit, which is the right way
# round: a wedge at noon is caught in ~25 minutes rather than 45.
day_wedge = (st.utc_now() - timedelta(minutes=30)).isoformat()
check("a daytime wedge is caught sooner than the old flat limit",
      run_watchdog({"last_check_at": day_wedge,
                    "next_poll_due": (st.utc_now() - timedelta(minutes=20)).isoformat()}),
      True)

check("older state without the marker still uses the flat limit",
      run_watchdog({"last_check_at": stale}), True)
check("...and a garbled marker does not disable the fallback",
      run_watchdog({"last_check_at": stale, "next_poll_due": "nonsense"}), True)

# A backoff outranks all of it: resting is not lateness.
check("a backoff still wins over an overdue poll",
      run_watchdog({"last_check_at": stale,
                    "next_poll_due": well_overdue,
                    "backoff_until": resting}), False)

print("\nThe same distinction, in state and in doctor")

s = dict(st._defaults())
check("a fresh state is not resting", st.backoff_remaining(s), 0.0)

st.note_backoff(s, 1800)
left = st.backoff_remaining(s)
check("a marked backoff has time left", 1700 < left <= 1800, True)

st.clear_backoff(s)
check("clearing it ends the rest", st.backoff_remaining(s), 0.0)

st.note_backoff(s, -60)      # already elapsed
check("an elapsed backoff reads as finished", st.backoff_remaining(s), 0.0)

s["backoff_until"] = "not a timestamp"
check("garbage does not read as resting", st.backoff_remaining(s), 0.0)

print("\nDoctor's staleness limit must follow the overnight cadence")
# It was derived from the daytime cycle alone, so once the overnight
# slowdown kicked in every ordinary gap looked wedged. On the night of
# 2026-08-16 real gaps reached 36 minutes against a 30-minute threshold —
# doctor reported a perfectly healthy watcher as broken, all night.

saved = config.NIGHT_START_HOUR, config.NIGHT_END_HOUR

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 0        # never night
day_interval, is_night = config.poll_interval_now()
check("by day the cadence is the normal cycle", is_night, False)
day_limit = max(day_interval * 2, 900) / 60

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = 0, 24       # always night
night_interval, is_night = config.poll_interval_now()
check("at night it is the slow one", is_night, True)
night_limit = max(night_interval * 2, 900) / 60

check("the night limit is more generous than the day one",
      night_limit > day_limit, True)
# The number that actually mattered: a 36-minute gap is normal overnight.
check("a real overnight gap is under the night limit", 36 < night_limit, True)
check("...and would have tripped the old day-derived limit",
      36 > max(config.POLL_INTERVAL_SECONDS * 3, 900) / 60, True)
# Still tight enough to catch a genuine overnight hang before the morning.
check("but a genuinely wedged watcher is still caught", night_limit < 90, True)

config.NIGHT_START_HOUR, config.NIGHT_END_HOUR = saved

print("\nAnd it must not scribble on the log used to diagnose real hangs")

# Running the suite used to append "restarting the watcher" to the live
# watchdog log, describing fixtures. That is the file you open at 3am when
# something has actually stopped, so it must contain only real events.
real_log = Path.home() / ".ep2026-watcher" / "logs" / "watchdog.log"
before = real_log.stat().st_mtime if real_log.exists() else None

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"last_check_at": (st.utc_now() - timedelta(minutes=90)).isoformat()}, f)
    fixture = f.name
own_log = fixture + ".log"
env = dict(os.environ)
env.update({
            # See test_hold_not_restarted.py: watchdog.sh correctly refuses
            # to restart anything past the stop date, so every check here that
            # expects a restart began failing on the calendar once the event
            # passed. Pinned forward — the logic under test is liveness, not
            # the date.
            "EP_STOP_AFTER": "2099-12-31",
    "EP_STATE_FILE": fixture,
    "EP_WATCHDOG_DRY_RUN": "1",
    "EP_WATCHDOG_LOG": own_log,
})
subprocess.run(["bash", str(REPO / "watchdog.sh")],
               capture_output=True, text=True, env=env, timeout=60)

check("it writes to the log it was given", os.path.exists(own_log), True)
check("and that log records the decision",
      "restarting the watcher" in open(own_log).read(), True)
after = real_log.stat().st_mtime if real_log.exists() else None
check("the operational log is untouched", after, before)

os.unlink(fixture)
os.unlink(own_log)


print("\nThe beacon is throttled, because it shares a quota with the alert")
# Found live on 2026-08-19: `doctor` got HTTP 429 publishing its own check,
# and the most recent beacon was 76 minutes old against a 90-minute deadline.
# The Mac was publishing on every handled reading — about eighteen an hour, or
# 450 a day — to answer a question asked once every 1.5 hours.
#
# The waste is not the point. ntfy is the FAST channel, the one that reaches a
# phone in seconds when a listing appears, and every beacon is a request that
# channel might have needed. Running the quota down proving the watcher is
# well, and finding it empty when it has something to say, is backwards.
import time as _time  # noqa: E402

from ep_watcher import liveness as _liveness  # noqa: E402

_was = _liveness._next_allowed
try:
    _liveness._next_allowed = 0.0
    check("a watcher that has just started announces itself at once",
          _liveness.due(), True)
    now = _time.time()
    _liveness._next_allowed = now + config.LIVENESS_INTERVAL_MINUTES * 60
    check("and then goes quiet", _liveness.due(now + 1), False)
    check("still quiet a minute later", _liveness.due(now + 60), False)
    check("but speaks again after the interval",
          _liveness.due(now + config.LIVENESS_INTERVAL_MINUTES * 60 + 1), True)

    # The margin that matters: enough beacons inside one silence window that
    # several can fail without the switch crying wolf.
    per_window = (config.MAC_SILENT_HOURS * 60) / config.LIVENESS_INTERVAL_MINUTES
    check(
        f"at least three beacons fit inside the {config.MAC_SILENT_HOURS}h "
        f"silence window (got {per_window:.0f})",
        per_window >= 3, True,
    )
    # And it must still be far below what was being sent before.
    check("which is far fewer than one per poll",
          config.LIVENESS_INTERVAL_MINUTES * 60 > config.POLL_INTERVAL_SECONDS, True)

    # A topic that is not configured must not be "due" forever, and must not
    # pretend it published.
    _was_topic, config.NTFY_TOPIC = config.NTFY_TOPIC, None
    try:
        check("with no topic there is nothing to publish",
              _liveness.publish("x"), False)
    finally:
        config.NTFY_TOPIC = _was_topic
finally:
    _liveness._next_allowed = _was


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
