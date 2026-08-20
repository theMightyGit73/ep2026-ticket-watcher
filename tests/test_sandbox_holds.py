"""The test sandbox must survive a config reload.

tests/_sandbox.py exists so that running one test file by hand cannot write
into the live state file, the live logs or the live browser profiles. Its
first version assigned config.LOG_DIR and config.DIAG_DIR directly, which
holds right up until something calls importlib.reload(config) — that
re-executes config.py from the top, restores every default, and silently
un-sandboxes the process while the `import _sandbox` line sits at the top of
the file still looking like protection.

That is not hypothetical. On 2026-08-20 a test that reloads config to check an
environment-driven setting went on to emit an event afterwards, and wrote a
fixture find — a listing in section STNDN9 at €366.39, on the weekend camping
page — into the real event log while the real watcher was running. The event
log's whole purpose is to be trusted for questions like "what appeared today,
and what did it cost", and a fabricated find in it is worse than a missing
one, because nothing about it looks wrong.

So the sandbox now works by setting the environment variables config reads,
which a reload cannot undo. This file pins that, because the failure is
invisible: everything passes either way, and the damage lands somewhere else
entirely.

Run with:  .venv/bin/python tests/test_sandbox_holds.py
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (the thing under test)

from ep_watcher import config, events  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


#: The directory the real watcher writes to. Nothing a test does may land here.
LIVE = Path.home() / ".ep2026-watcher"


def under_live(path) -> bool:
    try:
        Path(path).resolve().relative_to(LIVE.resolve())
        return True
    except (ValueError, OSError):
        return False


def writable_paths():
    """Every path the watcher can write, asked fresh from config each time."""
    return {
        "state file": config.STATE_FILE,
        "push quota": config.STATE_FILE.parent / "push-quota.json",
        "log dir": config.LOG_DIR,
        "event log": events.path(),
        "diagnostics": config.DIAG_DIR,
        "watch profile": config.PROFILE_DIR,
        "buy profile": config.BUY_PROFILE_DIR,
    }


print("\nNothing writable points at the live directory")

for label, path in writable_paths().items():
    check(f"{label} is outside {LIVE}", under_live(path), False)


print("\nAnd it stays that way after a config reload")

# The exact sequence that broke it. A test doing this is doing something
# perfectly reasonable — checking a setting that is read from the environment
# — and must not have to know that it also disarms the sandbox.
importlib.reload(config)

for label, path in writable_paths().items():
    check(f"{label} survives the reload", under_live(path), False)

# Twice, because a redirect that is consumed on first use would pass the check
# above and fail on the second reload.
importlib.reload(config)
importlib.reload(config)
for label, path in writable_paths().items():
    check(f"{label} survives repeated reloads", under_live(path), False)


print("\nThe event log a hand-run writes to is a real, writable file")

# Sandboxing by pointing somewhere that does not work would pass every check
# above and quietly break every test that emits. So prove the redirect is
# usable, not merely different.
events.emit("selftest", note="written by tests/test_sandbox_holds.py")
check_true("the sandboxed event log was created", events.path().exists())
check("and it is not the live one", under_live(events.path()), False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
