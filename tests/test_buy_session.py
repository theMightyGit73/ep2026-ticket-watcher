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
    # Wording changed on 2026-08-19 when the baseline stopped being a
    # hardcoded list and became the watcher's own signed-out profile.
    check_true("and says why",
               "signed-out profile" in ev["reason"] or "anonymous" in ev["reason"])

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
    check_true("and says a cookie lapsed",
               "lapsed" in ev["reason"] or "expired" in ev["reason"])

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

print("\nA profile whose only account cookie is session-scoped cannot be judged")
# Reversed on 2026-08-19. A session-scoped cookie is dropped by Chrome on
# exit, so it cannot answer "is this profile still signed in" between runs —
# there is nothing durable left to compare against. Saying so is honest;
# claiming it is signed in would be a guess that survives exactly until the
# browser closes.
with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof", dict(ANON, **{"SESSION": None}))
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    rec = buyer.record_signed_in_fingerprint(root)
    check("the session cookie is recorded", rec["auth_cookies"], ["SESSION"])
    check("but nothing durable is watched", rec["persistent_cookies"], [])
    ev = buyer.session_evidence(root)
    check("so the verdict is 'cannot tell', not a guess", ev["signed_in"], None)
    check_true("and it says to sign in again", "sign-in" in ev["reason"])

print("\nAn incomplete anonymous list must not invent an account")
# Caught on 2026-08-19 against the real profiles. KNOWN_ANONYMOUS_COOKIES was
# written by hand from one partial sample, so fifteen ordinary anonymous
# names — SID, TMUO, eps_sid, tmp_id and the rest — were missing from it. A
# buying profile that had never signed in was therefore reported as SIGNED
# IN. That is the dangerous direction: doctor goes green, the startup warning
# disappears, and the first anyone knows is a listing not being held.
#
# The fix is to diff against the watcher's own profile, which is on the same
# machine, always current, and guaranteed signed out.
UNLISTED = {"SID": FAR, "TMUO": FAR, "eps_sid": FAR, "tmp_id": FAR, "sticky": FAR}

with tempfile.TemporaryDirectory() as tmp:
    signed_out = make_profile(Path(tmp) / "watcher", dict(ANON, **UNLISTED))
    buying = make_profile(Path(tmp) / "buy", dict(ANON, **UNLISTED))
    buyer.SESSION_FILE = Path(tmp) / "no-fingerprint.json"

    real_profile_dir = buyer.config.PROFILE_DIR
    try:
        buyer.config.PROFILE_DIR = signed_out
        ev = buyer.session_evidence(buying)
        check("a profile matching the signed-out one is NOT signed in",
              ev["signed_in"], False)
        check_true("and says the cookies are all ones a signed-out profile has",
                   "signed-out profile also has" in ev["reason"])

        # A genuine account cookie, absent from the signed-out profile, still
        # reads as signed in.
        genuine = make_profile(Path(tmp) / "real", dict(ANON, **UNLISTED,
                                                       **{"tm-identity": FAR}))
        ev = buyer.session_evidence(genuine)
        check("a cookie the signed-out profile lacks IS evidence",
              ev["signed_in"], True)

        # And the fingerprint must not record the anonymous ones as the
        # account's, or every later check compares against noise.
        rec = buyer.record_signed_in_fingerprint(genuine)
        check("only the genuine cookie is recorded", rec["auth_cookies"], ["tm-identity"])

        # The baseline itself.
        check("the baseline is read from the watcher's profile",
              buyer.anonymous_baseline(), set(dict(ANON, **UNLISTED)))
    finally:
        buyer.config.PROFILE_DIR = real_profile_dir

# With no watcher profile to read, it degrades to the hardcoded list rather
# than raising.
real_profile_dir = buyer.config.PROFILE_DIR
try:
    buyer.config.PROFILE_DIR = Path("/nonexistent-profile-dir")
    check("a missing baseline is empty, not an exception",
          buyer.anonymous_baseline(), set())
finally:
    buyer.config.PROFILE_DIR = real_profile_dir


print("\nSession cookies vanish when Chrome closes, and that is not a sign-out")
# Measured on the real profile, 2026-08-19. Of fourteen cookies recorded at
# sign-in, TMAUO and ma.SID carried no expiry — Chrome drops those on exit,
# which it does after every securing attempt. Requiring all fourteen reported
# a perfectly good profile as SIGNED OUT the first time the browser was used.
with tempfile.TemporaryDirectory() as tmp:
    at_signin = dict(ANON, **{"id-token": FAR, "ma.SID": None, "TMAUO": None})
    root = make_profile(Path(tmp) / "prof", at_signin)
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    rec = buyer.record_signed_in_fingerprint(root)
    check("the session-only cookies are recorded but not watched",
          set(rec["auth_cookies"]) - set(rec["persistent_cookies"]),
          {"ma.SID", "TMAUO"})
    check_true("and the durable one is watched", "id-token" in rec["persistent_cookies"])

    # Chrome exits: the session cookies go, the persistent one stays.
    after_restart = make_profile(Path(tmp) / "prof", dict(ANON, **{"id-token": FAR}))
    ev = buyer.session_evidence(after_restart)
    check("still signed in after the browser has been closed", ev["signed_in"], True)

    # Losing the durable one IS a sign-out.
    make_profile(Path(tmp) / "prof", ANON)
    ev = buyer.session_evidence(Path(tmp) / "prof")
    check("but losing the durable cookie is", ev["signed_in"], False)
    check_true("and it names what went", "id-token" in ev["reason"])

print("\nAnalytics must not be mistaken for the account")
# Google Analytics mints a per-property cookie once you are past a sign-in, so
# it looked like an account cookie. It carried a 2027 expiry, and reporting
# the longest-lived one announced the session was good for 400 days — as wrong
# as the two hours it replaced.
check_true("a per-property GA cookie is analytics", buyer._is_analytics("_ga_MNQMF2C2CB"))
check_true("so is _gcl_au", buyer._is_analytics("_gcl_au"))
check_true("and permutive-id", buyer._is_analytics("permutive-id"))
check("but id-token is not", buyer._is_analytics("id-token"), False)
check("nor is ma.SID", buyer._is_analytics("ma.SID"), False)

with tempfile.TemporaryDirectory() as tmp:
    root = make_profile(Path(tmp) / "prof",
                        dict(ANON, **{"id-token": SOON, "_ga_MNQMF2C2CB": FAR}))
    buyer.SESSION_FILE = Path(tmp) / "buy-session.json"
    rec = buyer.record_signed_in_fingerprint(root)
    check("analytics is excluded from the account cookies",
          [n for n in rec["auth_cookies"] if n.startswith("_ga")], [])
    ev = buyer.session_evidence(root)
    check_true("so the reported lapse is the account's, not the tracker's",
               ev["days_left"] <= 3)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
