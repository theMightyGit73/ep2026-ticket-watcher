"""Redirect every writable path to a temp directory, for tests run by hand.

run_tests.sh already sandboxes the state file, the logs, the diagnostics and
both browser profiles, on the stated grounds that most tests handle it
themselves and "most" is not a guarantee worth betting the running watcher on.
That protection disappears the moment somebody runs one test file directly —
which is exactly what run_tests.sh prints as the way to investigate a failure.

It stopped being theoretical on 2026-08-20, the day the JSONL event log
arrived. Running tests/test_ticket_priority.py by hand wrote two fixture finds
into the REAL event log: a weekend-camping listing filed under early-entry, at
a price and section the live watcher never saw. Anyone reading that log later
would have found a find that never happened, in a file whose entire purpose is
to be trusted for exactly those questions. It happened a second time the same
evening — a fixture listing in section STNDN9 at €366.39, written into the
live log while the real watcher was running — which is what prompted the
rewrite below.

## Why this sets environment variables rather than module attributes

The first version assigned `config.LOG_DIR` and `config.DIAG_DIR` directly.
That works exactly until something calls `importlib.reload(config)`, which
re-executes config.py from the top and restores every default — silently
un-sandboxing the process, with the import line still sitting at the top of
the file looking like protection. Several tests reload config to check
environment-driven settings, and one of them emits events afterwards.

Environment variables survive a reload, because reading them is how config
builds those paths in the first place. So this sets the variables, and a test
may reload config as often as it likes without losing the redirect.

The variables are only set when absent, so run_tests.sh's own sandbox and any
deliberate override still win.

## What has to be covered

Everything the watcher can write, not just the log that caused the incident:

  * EP_STATE_FILE      — state.json, and push-quota.json beside it
  * EP_LOG_DIR         — watcher.log and events.jsonl
  * EP_DIAG_DIR        — find-*.json and the screenshots
  * EP_PROFILE_DIR     — the watching browser's Chrome profile
  * EP_BUY_PROFILE_DIR — the buying browser's, which holds the sign-in

The last two matter most and are the least likely to be exercised: one of the
functions reachable from a test is release_buying_browser(), which pkills the
browser a held ticket lives in.
"""

import os
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="ep-test-sandbox-"))

#: Variable -> the path it should point at inside the sandbox.
_PATHS = {
    "EP_STATE_FILE": _ROOT / "state.json",
    "EP_LOG_DIR": _ROOT / "logs",
    "EP_DIAG_DIR": _ROOT / "diagnostics",
    "EP_PROFILE_DIR": _ROOT / "chrome-profile",
    "EP_BUY_PROFILE_DIR": _ROOT / "chrome-profile-buy",
}

for _var, _value in _PATHS.items():
    os.environ.setdefault(_var, str(_value))

# config may already be imported — several tests do `from ep_watcher import
# config` above their `import _sandbox`, and import order in a standalone
# script is easy to get wrong by accident. Setting the variables above only
# helps a module that has not read them yet, so anything already loaded is
# reloaded here to pick them up.
if "ep_watcher.config" in sys.modules:
    import importlib

    importlib.reload(sys.modules["ep_watcher.config"])
