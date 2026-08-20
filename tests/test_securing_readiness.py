"""Saying whether securing can actually work, at the one time it is cheap to.

An armed-but-broken buying session fails exactly once: at the moment a real
listing appears, which is the moment there is no time to investigate. So the
watcher states the answer on every start, and `doctor` states it too — and
the two must give the SAME answer, or one of them is training David to
believe the other.

Until 2026-08-19 the startup banner asked the wrong question entirely. It
tested whether the buying profile's Cookies database existed:

    signed_in = (config.BUY_PROFILE_DIR / "Default" / "Cookies").exists()

That is a question with a reassuring answer and almost no meaning. A
signed-OUT ticketmaster.ie profile carries 33 cookies, so the file appears the
moment the buying browser has loaded a single page. A profile that had been
signed in and was since signed out — the exact rot the banner exists to catch
— passed it in silence, and `doctor` two screens away said the opposite.

Run with:  .venv/bin/python tests/test_securing_readiness.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer, config, engine  # noqa: E402
from ep_watcher import __main__ as cli  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
FAR = datetime.now(timezone.utc) + timedelta(days=30)


def make_profile(root, cookies):
    """A throwaway Chrome-shaped profile holding exactly these cookies."""
    default = root / "Default"
    default.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(default / "Cookies"))
    conn.execute("DROP TABLE IF EXISTS cookies")
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER,"
        " is_httponly INTEGER)"
    )
    for name, expiry in cookies.items():
        micros = int((expiry - CHROME_EPOCH).total_seconds() * 1_000_000) if expiry else 0
        conn.execute("INSERT INTO cookies VALUES (?,?,?,0)",
                     ("www.ticketmaster.ie", name, micros))
    conn.commit()
    conn.close()
    return root


# The real signed-out cookie set, as recorded from the watcher's own profile.
# Every one of these proves nothing about an account, and there are enough of
# them that the Cookies file plainly exists.
ANON = {name: FAR for name in buyer.KNOWN_ANONYMOUS_COOKIES}
ACCOUNT = dict(ANON, **{"id-token": FAR, "ma.SID": FAR, "TMAUO": FAR})


def banner(secure_on, profile, session_file):
    """The banner lines for one configuration, with nothing global left set."""
    was_flag, was_dir = config.SECURE_ON_FIND, config.BUY_PROFILE_DIR
    was_session, was_watcher = buyer.SESSION_FILE, config.PROFILE_DIR
    try:
        config.SECURE_ON_FIND = secure_on
        config.BUY_PROFILE_DIR = profile
        buyer.SESSION_FILE = session_file
        # The live watcher profile is the signed-out baseline. Point it at a
        # directory that does not exist so these checks cannot depend on
        # whatever is on the machine running them.
        config.PROFILE_DIR = profile.parent / "no-such-watcher-profile"
        return cli.securing_banner()
    finally:
        config.SECURE_ON_FIND, config.BUY_PROFILE_DIR = was_flag, was_dir
        buyer.SESSION_FILE, config.PROFILE_DIR = was_session, was_watcher


def warning(secure_on, profile, session_file):
    """engine.securing_warning() for one configuration, globals restored.

    The hourly counterpart to banner(). Both ask session_evidence() the same
    question; this test exists to keep them answering it the same way.
    """
    was_flag, was_dir = config.SECURE_ON_FIND, config.BUY_PROFILE_DIR
    was_session, was_watcher = buyer.SESSION_FILE, config.PROFILE_DIR
    try:
        config.SECURE_ON_FIND = secure_on
        config.BUY_PROFILE_DIR = profile
        buyer.SESSION_FILE = session_file
        config.PROFILE_DIR = profile.parent / "no-such-watcher-profile"
        return engine.securing_warning()
    finally:
        config.SECURE_ON_FIND, config.BUY_PROFILE_DIR = was_flag, was_dir
        buyer.SESSION_FILE, config.PROFILE_DIR = was_session, was_watcher


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    session_file = tmp / "buy-session.json"

    print("\nOff is off, and says so without qualification")
    lines = banner(False, make_profile(tmp / "unused", ACCOUNT), session_file)
    check("one line only", len(lines), 1)
    check_true("saying securing is off", "off" in lines[0].lower())
    check_true("and that this means notify-only", "notify only" in lines[0])

    print("\nThe regression: a signed-OUT profile with a full cookie jar")
    # This is the case the old check passed. The Cookies file exists and holds
    # 19 real ticketmaster.ie cookies — and not one of them is an account.
    out = tmp / "signed-out"
    make_profile(out, ANON)
    check_true("the old test would have said this profile was fine",
               (out / "Default" / "Cookies").exists())
    lines = banner(True, out, session_file)
    body = "\n".join(lines)
    check_true("securing is reported as ON", "Securing: ON" in body)
    check_true("but the profile is called out as NOT signed in",
               "NOT signed in" in body)
    check_true("with the command that fixes it", "login-buy" in body)
    # And the two screens must agree, which is the property that was broken.
    was_dir, was_session, was_watcher = (
        config.BUY_PROFILE_DIR, buyer.SESSION_FILE, config.PROFILE_DIR)
    try:
        config.BUY_PROFILE_DIR = out
        buyer.SESSION_FILE = session_file
        config.PROFILE_DIR = tmp / "no-such-watcher-profile"
        check("doctor's own evidence agrees it is signed out",
              buyer.session_evidence(out)["signed_in"], False)
    finally:
        config.BUY_PROFILE_DIR, buyer.SESSION_FILE, config.PROFILE_DIR = (
            was_dir, was_session, was_watcher)

    print("\nA profile that has never existed at all")
    lines = banner(True, tmp / "never-created", session_file)
    body = "\n".join(lines)
    check_true("is also reported as not signed in", "NOT signed in" in body)
    check_true("and names login-buy as the fix", "login-buy" in body)

    print("\nA genuinely signed-in profile is confirmed, not merely tolerated")
    good = tmp / "signed-in"
    make_profile(good, ACCOUNT)
    # Record the fingerprint the way login-buy does, at the moment a human
    # says "I have just signed in" — which is the only moment anyone can know
    # which cookie names carry the account.
    was_dir, was_session, was_watcher = (
        config.BUY_PROFILE_DIR, buyer.SESSION_FILE, config.PROFILE_DIR)
    try:
        config.BUY_PROFILE_DIR = good
        buyer.SESSION_FILE = session_file
        config.PROFILE_DIR = tmp / "no-such-watcher-profile"
        record = buyer.record_signed_in_fingerprint(good)
        check_true("the account cookies were identified",
                   "id-token" in record["auth_cookies"])
        check("and none of the anonymous ones were mistaken for them",
              [n for n in record["auth_cookies"] if n in buyer.KNOWN_ANONYMOUS_COOKIES],
              [])
    finally:
        config.BUY_PROFILE_DIR, buyer.SESSION_FILE, config.PROFILE_DIR = (
            was_dir, was_session, was_watcher)

    lines = banner(True, good, session_file)
    body = "\n".join(lines)
    check_true("the banner confirms the session", "signed in —" in body)
    check("and raises no warning at all", "⚠" in body, False)

    print("\nSigned in, then signed out again — the silent rot")
    # Cookies expire, Ticketmaster invalidates them, a profile reset wipes
    # them. All three look identical from outside and all three first show up
    # as a listing appearing and not being held.
    make_profile(good, ANON)          # same profile, account cookies gone
    lines = banner(True, good, session_file)
    body = "\n".join(lines)
    check_true("the banner now says it is NOT signed in", "NOT signed in" in body)
    check_true("and names the cookies that went missing",
               "id-token" in body or "gone" in body)

    print("\nAnd the same rot, an hour into a fortnight, reaches the inbox")
    # The banner above only ever speaks at startup. This run lasts two weeks,
    # so the cookies can lapse on day nine — long after the only sentence that
    # would have mentioned it scrolled off the log. The hourly report asks the
    # same question again, of the same evidence, so the answer cannot drift
    # between the two.
    check_true("armed and signed out warns in the hourly report",
               "SIGNED OUT" in warning(True, good, session_file))
    check_true("and says how to fix it",
               "login-buy" in warning(True, good, session_file))

    # The three quiet cases. Each is silence for a different reason, and
    # getting any of them wrong would put a false alarm in every hourly email
    # — which is how a warning stops being read.
    make_profile(good, ACCOUNT)       # signed in again
    check("a signed-in profile says nothing", warning(True, good, session_file), "")
    check("securing switched off says nothing",
          warning(False, good, session_file), "")
    # "Cannot tell" is not "signed out", and only the second of those may
    # speak. session_evidence() answers None when a sign-in was recorded but
    # named no lasting cookie to watch — there is then genuinely nothing to
    # check against, and this project has already been bitten once by reading
    # that silence as a no.
    vague = tmp / "vague"
    make_profile(vague, ACCOUNT)
    vague_session = tmp / "vague-session.json"
    vague_session.write_text(json.dumps({"persistent_cookies": [], "auth_cookies": []}))
    check("cannot-tell stays quiet rather than crying wolf",
          warning(True, vague, vague_session), "")

    # But a buying profile that has never existed at all is not a "cannot
    # tell" — it is a definite no, and the loudest one available.
    check_true("a profile that was never created does warn",
               "SIGNED OUT" in warning(True, tmp / "never-made", session_file))

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
