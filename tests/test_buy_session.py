"""Knowing whether the buying profile is still signed in.

This check decides whether the watcher believes it can hold a ticket, and it
was originally `"sign out" in page_text or "my account" in page_text`, copied
from the watcher's own login command. On 2026-08-19 that was tested against
every page capture the watcher has ever taken — nine of them, all from a
signed-OUT profile — and it contains none of those strings. Nor "sign in".
Ticketmaster puts the account control somewhere Playwright's flattened
inner_text cannot reach, so the test would have answered "not signed in" for a
perfectly good session, and the buyer would have refused to act on the first
real listing after David signed in correctly.

Cookies replace it, but presence alone is not the answer either: the
signed-out watcher profile already carries 33 ticketmaster.ie cookies, every
one of them analytics or consent. What matters is WHICH names are there, and
the only moment anyone can know that for certain is the moment a human says
"I have just signed in" — which is what login-buy now records.

Run with:  .venv/bin/python tests/test_buy_session.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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


CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def chrome_us(when):
    return int((when - CHROME_EPOCH).total_seconds() * 1_000_000)


def make_profile(root, cookies):
    """A throwaway Chrome-shaped profile holding exactly these cookies."""
    default = root / "Default"
    default.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(default / "Cookies"))
    # Rebuilt rather than appended to, so a test can rewrite a profile in
    # place to represent the same session at a later moment.
    conn.execute("DROP TABLE IF EXISTS cookies")
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER,"
        " is_httponly INTEGER)"
    )
    for name, expiry in cookies.items():
        conn.execute(
            "INSERT INTO cookies VALUES (?,?,?,0)",
            ("www.ticketmaster.ie", name, chrome_us(expiry) if expiry else 0),
        )
    conn.commit()
    conn.close()
    return root


FAR = datetime.now(timezone.utc) + timedelta(days=30)
SOON = datetime.now(timezone.utc) + timedelta(days=2)
PAST = datetime.now(timezone.utc) - timedelta(days=1)

ANON = {"_ga": FAR, "OptanonConsent": FAR, "BID": FAR, "LANGUAGE": FAR}
SIGNED_IN = dict(ANON, **{"SESSION": FAR, "identity.session": FAR})


print("\nThe flattened page text cannot answer this, which is why cookies do")
# The evidence for the rewrite, kept as a test so nobody reinstates the old
# check. These are the real captures the watcher has taken, all signed out.
diag = Path.home() / ".ep2026-watcher" / "diagnostics"
caps = sorted(diag.glob("find-*.txt")) + sorted(diag.glob("probe-*.txt"))
if caps:
    hits = sum(
        1 for f in caps
        for word in ("sign out", "my account", "sign in")
        if word in f.read_text(errors="replace").lower()
    )
    check(f"no account text in any of {len(caps)} real page captures", hits, 0)
else:
    print("  [ -- ]  no captures on this machine to check against")


print("\nAn anonymous profile is not a signed-in one")
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", ANON)
    buyer.SESSION_FILE = Path(tmp) / "missing.json"
    ev = buyer.session_evidence(root)
    check("recognised as signed out", ev["signed_in"], False)
    check_true("and says why", "anonymous" in ev["reason"])

print("\nA missing profile is a definite no, not a shrug")
with tempfile.TemporaryDirectory() as tmp:
    ev = buyer.session_evidence(Path(tmp) / "never-created")
    check("signed_in is False", ev["signed_in"], False)
    check_true("and names the fix", "login-buy" in ev["reason"])

print("\nA profile with no cookie database at all")
with tempfile.TemporaryDirectory() as tmp:
    empty = Path(tmp) / "prof"
    (empty / "Default").mkdir(parents=True)
    ev = buyer.session_evidence(empty)
    check("signed_in is False", ev["signed_in"], False)
    check_true("and says the profile is bare", "no ticketmaster cookies" in ev["reason"])

print("\nSigning in records which cookies carry the account")
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", SIGNED_IN)
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    rec = buyer.record_signed_in_fingerprint(root)
    check("the analytics cookies are excluded",
          rec["auth_cookies"], ["SESSION", "identity.session"])
    check("and the whole set is counted", rec["cookie_count"], len(SIGNED_IN))
    check_true("and it is written down for later", buyer.SESSION_FILE.exists())
    saved = json.loads(buyer.SESSION_FILE.read_text())
    check("what was written matches what was returned",
          saved["auth_cookies"], rec["auth_cookies"])

    print("\nAnd afterwards the session can be judged without a browser")
    ev = buyer.session_evidence(root)
    check("signed in", ev["signed_in"], True)
    check_true("expiry is known", ev["days_left"] is not None)
    check_true("and it is the soonest of the account cookies",
               29 <= ev["days_left"] <= 30)

    print("\nLosing an account cookie is being signed out")
    make_profile(Path(tmp) / "prof2", ANON)
    ev = buyer.session_evidence(Path(tmp) / "prof2")
    check("signed_in is False", ev["signed_in"], False)
    check_true("and it names what went missing",
               "SESSION" in ev["reason"] or "identity" in ev["reason"])

print("\nAn expiring session is caught before it lapses, not after")
# The whole point. Expiry is silent, and its first symptom would otherwise be
# a listing appearing and not being held.
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", dict(ANON, **{"SESSION": SOON}))
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    buyer.record_signed_in_fingerprint(root)
    ev = buyer.session_evidence(root)
    check("still signed in", ev["signed_in"], True)
    check_true("but with days left reported", 1 <= ev["days_left"] <= 2)
    check_true("which doctor treats as a warning", ev["days_left"] <= 3)

print("\nAn expired session is signed out, whatever the cookie says")
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", dict(ANON, **{"SESSION": FAR}))
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    buyer.record_signed_in_fingerprint(root)
    # The cookie is still named in the profile, but its expiry has passed.
    make_profile(Path(tmp) / "prof", dict(ANON, **{"SESSION": PAST}))
    ev = buyer.session_evidence(Path(tmp) / "prof")
    check("signed_in is False", ev["signed_in"], False)
    check_true("and says it expired", "expired" in ev["reason"])

print("\nReading cookies must work while Chrome has the file open")
# The database is copied before being read. A check that only works when the
# browser is shut is no check at all — the watcher's browser is always open.
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", SIGNED_IN)
    db = root / "Default" / "Cookies"
    holder = sqlite3.connect(str(db))
    holder.execute("BEGIN EXCLUSIVE")
    got = buyer.profile_cookies(root)
    holder.rollback()
    holder.close()
    check("cookies still readable under an exclusive lock", len(got), len(SIGNED_IN))

print("\nA corrupt or unreadable database degrades, it does not raise")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "prof"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Cookies").write_text("this is not a database")
    check("no cookies rather than an exception", buyer.profile_cookies(root), {})
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    ev = buyer.session_evidence(root)
    check("and the verdict is a definite signed-out", ev["signed_in"], False)

print("\nA session-only cookie has no expiry, and that is fine")
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", dict(ANON, **{"SESSION": None}))
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    buyer.record_signed_in_fingerprint(root)
    ev = buyer.session_evidence(root)
    check("still counted as signed in", ev["signed_in"], True)
    check("with no expiry to report", ev["days_left"], None)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
