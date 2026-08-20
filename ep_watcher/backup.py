"""Copy the things that live outside the repository and cannot be recreated.

Everything the watcher needs to keep working across a restart is in
``~/.ep2026-watcher/``, and none of it is in git — correctly, because it holds
secrets and a browser session. The consequence is that the four files this
project would most hate to lose have no copy anywhere:

* ``env`` — the Gmail app password and the ntfy topic. Without it the watcher
  refuses to start, because a watcher that cannot mail is worse than none.
* ``chrome-profile-buy`` — the signed-in Ticketmaster session. Recreating it
  means a human at a keyboard doing ``login-buy``, which is exactly the thing
  this project exists to not need at the moment a listing appears.
* ``state.json`` — which listings have been alerted on, how many blocks each
  connection has drawn, and whether a ticket is being held right now.
* ``buy-session.json`` — the cookie fingerprint recorded at sign-in, which is
  what makes "is the buying profile still signed in?" an exact question
  rather than a guess.

Ad-hoc copies of two of them already existed (``env.bak-1787144694`` and
friends), made by hand at moments when somebody was nervous. That is the
right instinct and the wrong mechanism: it protects the files you happened to
think about, on the days you happened to think about it.

Only the session-carrying parts of the browser profile are copied. The profile
on disk is 161 MB and essentially all of that is ``Cache`` and ``Code Cache``
— disposable by construction, since Chrome rebuilds both on demand. What
actually carries the sign-in is a few megabytes.

One honest limitation, stated here because a backup you believe in wrongly is
worse than none: on macOS Chrome encrypts its cookie values with a key held in
the login Keychain, not in the profile. These copies therefore restore only on
this Mac, under this user. That is enough for every failure this is meant to
survive — a bad profile reset, a mistaken ``rm``, a corrupted state file — and
not enough to move the session to another machine.
"""

import os
import shutil
import time
from pathlib import Path

from . import config
from .state import stamp

#: Outside ``~/.ep2026-watcher/`` on purpose. The likeliest way to lose the
#: originals is a command aimed at that directory, and a backup living inside
#: the thing it protects goes with it.
DEFAULT_ROOT = Path.home() / ".ep2026-watcher-backups"

#: How many snapshots to keep. A fortnight of daily runs would be fourteen;
#: seven covers every "it was working last week" question worth asking and
#: keeps the total under a few hundred megabytes.
KEEP = int(os.environ.get("EP_BACKUP_KEEP", "7"))

#: Loose files worth copying, relative to the runtime directory.
FILES = ("env", "state.json", "buy-session.json", "push-quota.json")

#: The parts of a Chrome profile that carry a session. Everything else is
#: cache, and copying 150 MB of it every day would make this expensive enough
#: to be worth switching off — which is how a backup stops existing.
PROFILE_PARTS = (
    "Local State",
    "Default/Cookies",
    "Default/Preferences",
    "Default/Local Storage",
    "Default/Session Storage",
)


def _runtime_dir() -> Path:
    """Where the live files are. Derived from the state file, not hardcoded,
    so a test pointing EP_STATE_FILE somewhere temporary backs that up
    instead of David's real session."""
    return config.STATE_FILE.parent


def _copy(src: Path, dst: Path) -> int:
    """Copy a file or a directory tree. Returns bytes written, 0 if absent."""
    if not src.exists():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    shutil.copy2(src, dst)
    return dst.stat().st_size


def prune(root: Path, keep: int = None) -> list:
    """Delete all but the newest `keep` snapshots. Returns what it removed."""
    keep = KEEP if keep is None else keep
    if not root.exists():
        return []
    snapshots = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    removed = []
    for old in snapshots[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def run(root: Path = None, keep: int = None) -> dict:
    """Take one snapshot. Returns {path, copied, skipped, bytes, pruned}.

    Never raises. This is called from the poll loop once a day, and a backup
    that can break the watcher is a worse bargain than no backup at all — the
    watcher's job is to catch a ticket, and nothing in here is allowed to
    interfere with that.
    """
    root = Path(root) if root else DEFAULT_ROOT
    runtime = _runtime_dir()
    out = {"path": None, "copied": [], "skipped": [], "bytes": 0, "pruned": [],
           "error": None}

    try:
        snapshot = root / time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        snapshot.mkdir(parents=True, exist_ok=True)
        # The archive holds a Gmail app password and a live session. Lock the
        # whole tree down before anything is written into it, not after.
        os.chmod(root, 0o700)
        os.chmod(snapshot, 0o700)

        for name in FILES:
            written = _copy(runtime / name, snapshot / name)
            if written:
                out["copied"].append(name)
                out["bytes"] += written
                os.chmod(snapshot / name, 0o600)
            else:
                out["skipped"].append(name)

        for part in PROFILE_PARTS:
            src = config.BUY_PROFILE_DIR / part
            written = _copy(src, snapshot / "chrome-profile-buy" / part)
            if written:
                out["copied"].append(f"chrome-profile-buy/{part}")
                out["bytes"] += written
            else:
                out["skipped"].append(f"chrome-profile-buy/{part}")

        out["path"] = snapshot
        out["pruned"] = prune(root, keep)
    except Exception as exc:
        # Reported, never raised. See the docstring.
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def describe(result: dict) -> str:
    """One block of text for a human, whether it worked or not."""
    if result["error"]:
        return f"[{stamp()}] backup FAILED: {result['error']}"
    megabytes = result["bytes"] / 1_048_576
    lines = [
        f"[{stamp()}] backed up {len(result['copied'])} item(s), "
        f"{megabytes:.1f} MB → {result['path']}"
    ]
    if result["skipped"]:
        lines.append(f"    not present, so not copied: {', '.join(result['skipped'])}")
    if result["pruned"]:
        lines.append(f"    pruned {len(result['pruned'])} old snapshot(s)")
    return "\n".join(lines)
