"""The logs must not grow for ever, and rotating them must not lose the writer.

Nothing rotated these until 2026-08-20. launchd appends to StandardOutPath for
the whole life of the job, macOS's newsyslog does not manage files under ~/,
and the watcher only ever prints — so watcher.log reached 2 MB in the first
eight days with nothing in the system able to stop it. That log is the first
thing anyone opens when the watcher has gone quiet, which makes an unbounded
one a diagnostic problem well before it is a disk problem.

The dangerous way to fix that is the obvious one. `mv watcher.log
watcher.log.1` looks like rotation and is a trap: launchd holds an open
descriptor on the inode, so the rename carries the live log away with it.
Every line the watcher prints afterwards lands in a file nobody is tailing,
while watcher.log sits at zero bytes looking exactly like a watcher that has
died — the single failure mode this whole project exists to make impossible.

So the property under test is not "the file got smaller". It is that a process
which already had the log open goes on writing to the file you are reading.

Everything here is offline: it runs the real watchdog.sh against a temporary
log directory and a state file that does not exist.

Run with:  .venv/bin/python tests/test_log_rotation.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def run_watchdog(log_dir, max_bytes):
    """Run the real watchdog.sh with nothing to do except rotate."""
    env = dict(os.environ)
    env.update({
        "EP_LOG_DIR": str(log_dir),
        "EP_LOG_MAX_BYTES": str(max_bytes),
        # No state file, so the watchdog rotates and then exits before it can
        # form any opinion about whether the watcher is healthy.
        "EP_STATE_FILE": str(log_dir / "no-such-state.json"),
        "EP_WATCHDOG_LOG": str(log_dir / "watchdog.log"),
    })
    subprocess.run(["bash", str(REPO / "watchdog.sh")],
                   capture_output=True, text=True, env=env, timeout=60)


with tempfile.TemporaryDirectory() as tmp:
    logs = Path(tmp)

    print("\nAn oversized log is rotated, and a small one is left alone")
    big = logs / "watcher.log"
    big.write_text("x" * 200_000)
    small = logs / "watcher.err.log"
    small.write_text("nothing much\n")

    run_watchdog(logs, max_bytes=100_000)

    check("the oversized log is emptied", big.stat().st_size, 0)
    check_true("and its contents are kept as .1", (logs / "watcher.log.1").exists())
    check("with everything that was in it",
          (logs / "watcher.log.1").stat().st_size, 200_000)
    check("the small log is untouched", small.read_text(), "nothing much\n")
    check("and is not archived", (logs / "watcher.err.log.1").exists(), False)

    print("\nA log under the limit is not rotated on the next run either")
    run_watchdog(logs, max_bytes=100_000)
    check("the emptied log stays empty rather than cycling",
          big.stat().st_size, 0)
    check("and the archive is not overwritten with nothing",
          (logs / "watcher.log.1").stat().st_size, 200_000)

    print("\nThe property that matters: launchd's open log survives it")
    # This is the whole reason rotation copies and truncates instead of
    # renaming. `held` stands in for launchd, which opened this file when it
    # started the watcher and will not open it again for a fortnight.
    live = logs / "live.log"
    live.write_text("y" * 200_000)
    held = open(live, "a")
    try:
        run_watchdog(logs, max_bytes=100_000)
        check("the live log was emptied", live.stat().st_size, 0)

        held.write("a line printed after the rotation\n")
        held.flush()

        # The test that a rename would fail. With `mv`, this line would be
        # sitting in live.log.1 and live.log would still be zero bytes.
        check("the writer's next line lands in the file being tailed",
              live.read_text(), "a line printed after the rotation\n")
        check_true("and not in the archive",
                   "printed after" not in (logs / "live.log.1").read_text())
    finally:
        held.close()

    print("\nRotation is bounded — one generation, never a growing pile")
    check("no second generation is created", (logs / "watcher.log.1.1").exists(), False)
    check("and no .2", (logs / "watcher.log.2").exists(), False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
