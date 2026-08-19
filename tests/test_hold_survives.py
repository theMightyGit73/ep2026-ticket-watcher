"""A live hold must survive everything else the project does.

Once a ticket is in a basket the clock is running and there is exactly one
way to lose it that is nobody's bad luck: closing the browser holding it.
Two separate paths did that, and both were found by reading rather than by
losing a ticket to them.

  1. `restart.sh` ran `pkill -f "ep2026-watcher/chrome-profile"`. `pkill -f`
     matches a substring of the whole command line, so that also matched
     chrome-profile-buy — the signed-in browser deliberately left open while
     a basket is held. restart.sh is what `doctor` prints as the fix for half
     its failure lines, so the repair for a wedged watcher was also the way
     to throw away the ticket it had just caught.

  2. Nothing ever closes the buying browser after a success, which is
     correct — closing it drops the basket. But Chrome takes an exclusive
     lock on a user-data-dir, so the NEXT find met a locked profile and
     failed with a Playwright message about singleton locks that said nothing
     about a ticket. Six real listings appeared on 2026-08-18; two inside one
     fifteen-minute hold window is an ordinary afternoon.

Run with:  .venv/bin/python tests/test_hold_survives.py
"""

import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


print("\nrestart.sh must kill the watcher's browser and spare the buying one")
# The pattern is read out of the script rather than restated here, so editing
# the script back to the dangerous form fails this test instead of quietly
# passing a copy of the old text.
script = (Path(__file__).resolve().parent.parent / "restart.sh").read_text()
found = re.search(r'pkill -f "([^"]+)"', script)
check_true("restart.sh still has exactly one pkill line", found is not None)

# pkill -f uses POSIX extended regular expressions, which Python's re reads
# compatibly for a pattern this simple.
pattern = re.compile(found.group(1)) if found else re.compile(r"$^")
WATCHER_CMDLINE = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
    "--disable-blink-features=AutomationControlled --window-position=-2400,-2400 "
    "--user-data-dir=/Users/davidcoyne/.ep2026-watcher/chrome-profile"
)
BUYING_CMDLINE = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
    "--disable-blink-features=AutomationControlled "
    "--user-data-dir=/Users/davidcoyne/.ep2026-watcher/chrome-profile-buy"
)
# A helper process carries more arguments after the profile, so the pattern
# must not depend on the profile being last on the line.
WATCHER_HELPER = WATCHER_CMDLINE + " --type=renderer --lang=en-GB"

check_true("it matches the watcher's own browser",
           pattern.search(WATCHER_CMDLINE) is not None)
check_true("including its helper processes",
           pattern.search(WATCHER_HELPER) is not None)
check("it does NOT match the buying browser — this is the whole point",
      pattern.search(BUYING_CMDLINE) is not None, False)
# The other scratch profiles are not holds and killing them is harmless, but
# they show the anchor is doing real work rather than special-casing "-buy".
for scratch in ("chrome-profile-check", "chrome-profile-probe"):
    line = BUYING_CMDLINE.replace("chrome-profile-buy", scratch)
    check(f"nor {scratch}", pattern.search(line) is not None, False)

# And the script must say what it left alone, rather than leaving a stray
# Chrome window as a mystery.
check_true("restart.sh reports an open buying browser rather than ignoring it",
           "chrome-profile-buy" in script and "left alone" in script)


print("\nprofile_in_use sees a browser holding the buying profile")
with tempfile.TemporaryDirectory() as tmp:
    profile = Path(tmp) / "chrome-profile-buy"
    profile.mkdir()
    check("an idle profile is not in use", buyer.profile_in_use(profile), False)

    # A stand-in for Chrome: any process whose command line carries the same
    # --user-data-dir argument. What is being tested is the detection, not
    # Chrome, and launching a real browser in a test would be both slow and
    # exactly the thing the watcher is careful about.
    stand_in = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         f"--user-data-dir={profile}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # pgrep reads the process table, which the OS populates a moment after
        # the fork; without this the test races the kernel rather than the code.
        for _ in range(40):
            if buyer.profile_in_use(profile):
                break
            time.sleep(0.1)
        check("a profile with a browser on it is in use",
              buyer.profile_in_use(profile), True)
        other = Path(tmp) / "chrome-profile"
        other.mkdir()
        check("and a different profile is not — the match is not a substring",
              buyer.profile_in_use(other), False)
    finally:
        stand_in.kill()
        stand_in.wait(timeout=10)

    check("once the browser is gone the profile is free again",
          buyer.profile_in_use(profile), False)


print("\nA second find while the first is still held is refused, not collided with")
started = []


class ExplodingSession:
    """Standing in for BuySession. Constructing one at all is the failure."""

    def __init__(self, *a, **kw):
        started.append(True)
        raise AssertionError("a second browser was opened on a locked profile")


real_in_use, real_session = buyer.profile_in_use, buyer.BuySession
try:
    buyer.profile_in_use = lambda *a, **kw: True
    buyer.BuySession = ExplodingSession
    hold = buyer.secure_in_thread(object(), object(), timeout_s=10)
finally:
    buyer.profile_in_use, buyer.BuySession = real_in_use, real_session

check("no browser was opened", started, [])
check("nothing was claimed as held", hold.secured, False)
check_true("and the reason says what is actually going on",
           "already open" in hold.reason)
check_true("saying it deferred to what is already held, not just that it is busy",
           "at least as important" in hold.reason)
# The alert has to be actionable at a glance, on a phone, under a countdown.
check_true("and telling him what to do about it",
           "close" in hold.reason.lower() or "finish" in hold.reason.lower())


print("\nThe guard must never be the reason a real listing goes unheld")
# profile_in_use answers False on any error, so a machine without pgrep, or a
# process table that cannot be read, lets the attempt go ahead and fail on its
# own terms rather than silently refusing every find.
real_run = subprocess.run
try:
    def boom(*a, **kw):
        raise OSError("pgrep is not installed here")

    subprocess.run = boom
    check("an unreadable process table answers 'not in use'",
          buyer.profile_in_use(Path("/nonexistent")), False)
finally:
    subprocess.run = real_run

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
