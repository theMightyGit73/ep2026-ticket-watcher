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

from ep_watcher import state as st  # noqa: E402

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

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
