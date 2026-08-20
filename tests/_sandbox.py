"""Redirect writable paths to a temp directory, for tests run by hand.

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
to be trusted for exactly those questions.

Importing this module is a no-op under run_tests.sh, where the environment
already points everywhere temporary. On a hand-run it quietly moves the
writes somewhere harmless.
"""

import os
import pathlib
import tempfile

from ep_watcher import config

if not os.environ.get("EP_LOG_DIR"):
    config.LOG_DIR = pathlib.Path(tempfile.mkdtemp(prefix="ep-test-logs-"))
if not os.environ.get("EP_DIAG_DIR"):
    config.DIAG_DIR = pathlib.Path(tempfile.mkdtemp(prefix="ep-test-diag-"))
