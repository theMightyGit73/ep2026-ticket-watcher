"""What lives outside the repository must have a copy somewhere.

Four things the watcher cannot work without are deliberately not in git — the
env file holding the Gmail app password, the signed-in Chrome profile,
state.json, and the cookie fingerprint recorded at sign-in. Correct, and it
left every one of them existing in exactly one place. Ad-hoc copies had
already appeared by hand (`env.bak-1787144694` and friends), which is the
right instinct expressed as the wrong mechanism: it protects the files
somebody happened to think about, on the days they happened to think about it.

The properties defended here are the ones that make a backup worth having:
it takes the session-carrying parts and not 150 MB of disposable cache, it
survives things being absent, it locks down a tree that holds a password, and
it lands somewhere a command aimed at the runtime directory cannot reach.

Everything is offline and temporary. Neither the real runtime directory nor
the real backup root is touched.

Run with:  .venv/bin/python tests/test_backup.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import backup, config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def make_runtime(root: Path) -> Path:
    """A runtime directory shaped like the real one, cache and all."""
    runtime = root / "runtime"
    (runtime).mkdir(parents=True)
    (runtime / "env").write_text("GMAIL_APP_PASSWORD=not-a-real-one\n")
    (runtime / "state.json").write_text('{"checks_total": 12}')
    (runtime / "buy-session.json").write_text('{"persistent_cookies": ["id-token"]}')
    (runtime / "push-quota.json").write_text('{"day": "2026-08-20", "count": 3}')

    profile = runtime / "chrome-profile-buy"
    (profile / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("cookie database")
    (profile / "Default" / "Preferences").write_text("{}")
    (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").write_text("ls")
    (profile / "Local State").write_text("{}")
    # The bulk of a real profile, and none of it worth a daily copy.
    (profile / "Default" / "Cache").mkdir(parents=True)
    (profile / "Default" / "Cache" / "big").write_bytes(b"z" * 400_000)
    (profile / "Default" / "Code Cache").mkdir(parents=True)
    (profile / "Default" / "Code Cache" / "bigger").write_bytes(b"z" * 800_000)
    return runtime


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    runtime = make_runtime(tmp)
    archive = tmp / "backups"

    was_state, was_profile = config.STATE_FILE, config.BUY_PROFILE_DIR
    config.STATE_FILE = runtime / "state.json"
    config.BUY_PROFILE_DIR = runtime / "chrome-profile-buy"
    try:
        print("\nA snapshot takes what cannot be recreated")
        result = backup.run(root=archive)
        check("it did not fail", result["error"], None)
        snapshot = result["path"]

        for name in ("env", "state.json", "buy-session.json", "push-quota.json"):
            check_true(f"{name} was copied", (snapshot / name).exists())
        check("the env file's contents came with it",
              (snapshot / "env").read_text(), "GMAIL_APP_PASSWORD=not-a-real-one\n")

        profile = snapshot / "chrome-profile-buy"
        check_true("the cookie database was copied",
                   (profile / "Default" / "Cookies").exists())
        check_true("and the local storage tree",
                   (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").exists())
        check_true("and Local State", (profile / "Local State").exists())

        print("\nAnd leaves behind the 150 MB that Chrome rebuilds on demand")
        check("the cache is not copied", (profile / "Default" / "Cache").exists(), False)
        check("nor the code cache", (profile / "Default" / "Code Cache").exists(), False)
        # 1.2 MB of cache sits in the source. If any of it were being copied
        # this number would not be small.
        check_true("so the snapshot stays small", result["bytes"] < 200_000)

        print("\nIt holds a password, so it is locked down")
        check("the archive root is owner-only",
              oct(archive.stat().st_mode & 0o777), "0o700")
        check("the snapshot directory too",
              oct(snapshot.stat().st_mode & 0o777), "0o700")
        check("and the env file inside it",
              oct((snapshot / "env").stat().st_mode & 0o777), "0o600")

        print("\nIt lands outside the directory it protects")
        # The likeliest way to lose the originals is a command aimed at the
        # runtime directory. A backup living inside it goes with them.
        check_true("the archive is not under the runtime directory",
                   runtime not in snapshot.parents)

        print("\nMissing pieces are reported, not fatal")
        (runtime / "env").unlink()
        (runtime / "chrome-profile-buy" / "Local State").unlink()
        result = backup.run(root=archive)
        check("it still succeeded", result["error"], None)
        check_true("and said the env file was absent", "env" in result["skipped"])
        check_true("and named the missing profile part",
                   any("Local State" in s for s in result["skipped"]))
        check_true("while still copying what was there",
                   "state.json" in result["copied"])

        print("\nA runtime directory that does not exist at all is survivable")
        config.BUY_PROFILE_DIR = tmp / "nothing-here"
        was = config.STATE_FILE
        config.STATE_FILE = tmp / "nowhere" / "state.json"
        try:
            result = backup.run(root=archive)
            check("no exception escaped", result["error"], None)
            check("and nothing was claimed as copied", result["copied"], [])
        finally:
            config.STATE_FILE = was
            config.BUY_PROFILE_DIR = runtime / "chrome-profile-buy"

        print("\nOld snapshots are pruned, newest kept")
        for stamp_name in ("20260101-000000", "20260102-000000", "20260103-000000"):
            (archive / stamp_name).mkdir(exist_ok=True)
        kept_before = sorted(d.name for d in archive.iterdir() if d.is_dir())
        backup.prune(archive, keep=2)
        kept = sorted(d.name for d in archive.iterdir() if d.is_dir())
        check("only two snapshots remain", len(kept), 2)
        check("and they are the two newest", kept, kept_before[-2:])
    finally:
        config.STATE_FILE, config.BUY_PROFILE_DIR = was_state, was_profile


print("\nThe daily clock")
now = datetime.now(timezone.utc)
check("a state that has never been backed up is due", st.backup_is_due({}), True)
check("one backed up an hour ago is not",
      st.backup_is_due({"last_backup_at": (now - timedelta(hours=1)).isoformat()}), False)
check("one backed up 25 hours ago is",
      st.backup_is_due({"last_backup_at": (now - timedelta(hours=25)).isoformat()}), True)

marked = {}
st.note_backup(marked)
check("and noting one makes it not due", st.backup_is_due(marked), False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
