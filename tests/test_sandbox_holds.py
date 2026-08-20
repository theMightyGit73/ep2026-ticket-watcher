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

print("\nNo test reloads config on top of settings it has already made")

# The same failure as above, in its other costume, and it is worth a static
# check because it is invisible at runtime — the reload succeeds, the test
# passes, and the setting it silently discarded is missed somewhere else.
#
# It has now happened twice in one evening. Once to the sandbox (a reload
# restored the live log paths and a fixture find was written into the real
# event log), and once to test_page_budget, where a reload restored the peak
# and night windows the file had pinned at the top — so it passed all
# afternoon and failed at 20:00 local, against code that had not changed.
#
# The rule: if a file assigns config.SOMETHING and then reloads config, it
# must assign it again afterwards. Anything else is a setting that looks
# applied and is not.
import ast  # noqa: E402

TESTS = Path(__file__).resolve().parent


def reload_lines(tree):
    """Line numbers of every importlib.reload(config) call."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "reload":
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Name) and arg.id == "config":
                out.append(node.lineno)
            # importlib.reload(sys.modules["ep_watcher.config"]) counts too.
            elif isinstance(arg, ast.Subscript):
                out.append(node.lineno)
    return out


def config_writes(tree):
    """{attribute: [line numbers]} for every `config.X = ...` in the file."""
    writes = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            for part in (target.elts if isinstance(target, ast.Tuple) else [target]):
                if (isinstance(part, ast.Attribute)
                        and isinstance(part.value, ast.Name)
                        and part.value.id == "config"):
                    writes.setdefault(part.attr, []).append(node.lineno)
    return writes


def assertion_lines(tree):
    """Line numbers of every check()/check_true() call in the file."""
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("check", "check_true")]


offenders = []
for path in sorted(TESTS.glob("test_*.py")):
    tree = ast.parse(path.read_text())
    reloads = reload_lines(tree)
    if not reloads:
        continue
    checks = assertion_lines(tree)
    # Only reloads that still have assertions after them can do harm. The
    # tidy-up reload in a finally: at the very end of a file discards
    # settings nothing will read again, and flagging it would train whoever
    # reads this to skim past a list of things that are fine.
    risky = [r for r in reloads if any(c > r for c in checks)]
    if not risky:
        continue
    last_reload = max(risky)
    for attr, lines in config_writes(tree).items():
        # Written before the last risky reload, never written again after it,
        # and an assertion follows. That setting is not in force for it.
        if min(lines) < last_reload and not any(l > last_reload for l in lines):
            offenders.append(f"{path.name}: config.{attr} set at line "
                             f"{min(lines)}, discarded by the reload at "
                             f"{last_reload}")

for line in offenders:
    print(f"        {line}")
check("no setting is discarded by a later reload", offenders, [])

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
