"""restart.sh must never close a browser with a ticket in it.

That script is what `doctor` prints as the fix for half its failure lines, so
it gets run at exactly the moments things are already going wrong — including,
inevitably, while a basket is live. It has one line that can destroy a ticket,
and this file is about that line.

The rule changed on 2026-08-20 and got harder. Before then an open buying
browser meant one thing, a hold waiting to be paid for, and leaving it alone
was simply correct. Now the browser is kept WARM — opened at watcher startup
and parked — so an open one is usually idle and belongs to the watcher about
to be replaced. Leaving that alone orphans it: it keeps the profile lock for
ever, the new watcher's warm browser cannot start, and the feature silently
degrades to the cold starts it exists to avoid.

So the two cases have to be told apart by state.json, and every way of failing
to tell them apart has to fail towards keeping the ticket. A wrong KEEP costs
a warm browser. A wrong KILL costs a ticket that was already caught.

Runs the real restart.sh in dry-run, so nothing is stopped or started.

Run with:  .venv/bin/python tests/test_restart_safety.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def verdict(state_body):
    """What would restart.sh do about the buying browser, given this state?"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        if isinstance(state_body, str):
            f.write(state_body)
        else:
            json.dump(state_body, f)
        path = f.name
    try:
        env = dict(os.environ)
        env.update({"EP_RESTART_DRY_RUN": "1", "EP_STATE_FILE": path})
        out = subprocess.run(["bash", str(REPO / "restart.sh")],
                             capture_output=True, text=True, env=env, timeout=60)
        for line in out.stdout.splitlines():
            if line.startswith("BUY_BROWSER="):
                return line.split("=", 1)[1].strip()
        return f"NO VERDICT (rc={out.returncode})"
    finally:
        os.unlink(path)


def missing_state_verdict():
    env = dict(os.environ)
    env.update({"EP_RESTART_DRY_RUN": "1",
                "EP_STATE_FILE": "/tmp/definitely-not-a-state-file-12345.json"})
    out = subprocess.run(["bash", str(REPO / "restart.sh")],
                         capture_output=True, text=True, env=env, timeout=60)
    for line in out.stdout.splitlines():
        if line.startswith("BUY_BROWSER="):
            return line.split("=", 1)[1].strip()
    return "NO VERDICT"


now = datetime.now(timezone.utc)

print("\nA live hold is untouchable")
check("a ticket held for another 8 minutes survives",
      verdict({"hold_until": (now + timedelta(minutes=8)).isoformat()}), "KEEP")
check("and one with seconds left still survives",
      verdict({"hold_until": (now + timedelta(seconds=90)).isoformat()}), "KEEP")

print("\nAn idle warm browser is this script's to clear")
check("no hold recorded", verdict({"hold_until": None}), "KILL")
check("no hold key at all", verdict({}), "KILL")
check("a hold whose window has passed",
      verdict({"hold_until": (now - timedelta(minutes=3)).isoformat()}), "KILL")

print("\nEvery way of not knowing fails towards keeping the ticket")
# Being wrong here in the cautious direction costs a warm browser and a slower
# securing attempt. Being wrong the other way costs a ticket already caught.
check("a timestamp that will not parse",
      verdict({"hold_until": "not a timestamp"}), "KEEP")
check("a hold_until of the wrong type", verdict({"hold_until": 12345}), "KEEP")
check("a state file that is not JSON", verdict("{ this is not json"), "KEEP")
check("an empty state file", verdict(""), "KEEP")
check("no state file on disk at all", missing_state_verdict(), "KEEP")

print("\nThe dry run really is dry")
# If this script could stop the watcher while under test, nobody would run the
# test — which is how the untested branch got there in the first place.
env = dict(os.environ)
env.update({"EP_RESTART_DRY_RUN": "1", "EP_STATE_FILE": "/tmp/nope.json"})
out = subprocess.run(["bash", str(REPO / "restart.sh")],
                     capture_output=True, text=True, env=env, timeout=60)
combined = out.stdout + out.stderr
check("it exits cleanly", out.returncode, 0)
check("and says it did nothing", "dry run" in combined, True)
for danger in ("Installing LaunchAgents", "Starting", "Checking it actually came up"):
    check(f"it never got as far as {danger!r}", danger in combined, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
