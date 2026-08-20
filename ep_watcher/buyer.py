"""Secure a resale listing in a basket. Never pay for it.

The watcher spent its first week being very good at the half of the job that
turned out not to be the hard half. It found six real listings on 2026-08-18
and alerted on every one; David reached none of them in time. His account of
why is specific and matches the data: by the time he has opened the page, set
the quantity and searched, the listing is either gone or refuses on the next
screen because it is sitting in somebody else's basket.

So this module closes that gap, and only that gap. It clicks into a listing
the moment the watcher sees one, puts it in a basket, and stops dead. It does
not enter payment details, does not confirm an order, and has no code path
that could. The hold is then David's to complete on the same machine — a
Ticketmaster basket lives in the session that created it, so the handoff is
"walk to this laptop", not "click a link on your phone".

Two browsers, deliberately
--------------------------
The watcher's own browser (config.PROFILE_DIR) stays signed OUT and does all
the polling. This one (config.BUY_PROFILE_DIR) is signed in and only ever
opens when a real listing exists. On 2026-08-18 that would have been six
openings against 140 polls, which is the ratio that keeps his account away
from the traffic that gets connections blocked.

What is verified and what is not
--------------------------------
The listing-row selectors below are built from the page text captured in the
find recordings of 2026-08-18 — the "Verified Resale Ticket" row, its section
line and its price. They have NOT been driven through to a basket against a
live listing, because no listing has been live since this was written. The
flow is written to fail loudly and harmlessly: every step that cannot find
what it expects records why and returns `secured=False`, and the ordinary
alert still goes out. Treat the first real find as the test.
"""

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Optional

from . import config

# ── Knowing whether the buying profile is signed in ──────────────────────────
#
# This was originally `"sign out" in page_text or "my account" in page_text`,
# copied from the watcher's own login command. On 2026-08-19 that was checked
# against every page capture the watcher has ever taken and found to be
# useless in both directions: not one of the nine recordings contains "sign
# out", "my account" OR "sign in". Ticketmaster does not put the account
# control anywhere that Playwright's flattened `inner_text` can see it, so
# the test would have answered "not signed in" for a perfectly good session —
# and the buyer would have refused to act on the first real listing after
# David had signed in correctly.
#
# Cookies are the honest signal, but presence alone is not enough either: the
# signed-OUT watcher profile already carries 33 ticketmaster.ie cookies, all
# of them analytics and consent. What distinguishes a signed-in profile is
# WHICH names are present, and the only moment anybody can know that for
# certain is the moment a human says "I have just signed in".
#
# So `login-buy` records the names it sees at that moment, and everything
# afterwards compares against that recording. A guess made once, by a human,
# beats a guess hard-coded by someone who has never seen the page.

#: Cookie names present on a signed-OUT ticketmaster.ie profile, read from the
#: watcher's own profile on 2026-08-19. Anything in this set proves nothing.
KNOWN_ANONYMOUS_COOKIES = {
    "mt.v", "_ga", "BID", "_scid", "_scid_r", "cto_bundle", "__gads", "__gpi",
    "LANGUAGE", "_au_1d", "OptanonConsent", "OptanonGroups", "__spdt",
    "eupubconsent-v2", "_gcl_au", "_fbp", "_uetvid", "_uetsid",
}

#: Recorded at sign-in, but worthless as evidence of one.
#:
#: These appear on a signed-in profile and not on the signed-out baseline, so
#: record_signed_in_fingerprint collects them as "account cookies" — and then
#: their absence is read as having been signed out. They are nothing of the
#: kind:
#:
#:   * KP_UIDz / KP_UIDz-ssn are Kasada's bot-detection tokens. They are
#:     reissued constantly and are cleared outright whenever the browser
#:     identity is refreshed, which this watcher does every 90 minutes on
#:     purpose.
#:   * ma.paramsToken and SOTC are short operational cookies the site reissues
#:     on the next page load. SOTC was observed carrying a two-hour expiry.
#:   * ma.LANGUAGE is a language preference.
#:
#: Measured on 2026-08-20, when doctor reported the buying profile signed out
#: while nine of its eleven recorded cookies were present and healthy —
#: including id-token with a month left. The two missing ones were the Kasada
#: pair. `login-auto` then went to sign in and found Ticketmaster serving an
#: ACCOUNT page rather than a sign-in form, which settled it: the session was
#: fine and the check was wrong.
#:
#: This is the second time this exact lesson has been learned. Commit 4938c25
#: stopped the session being judged by the EXPIRY of a cookie designed to
#: churn; it left the PRESENCE test judging by the same cookies. Both halves
#: are needed, and the cost of getting it wrong is not a failed hold — securing
#: attempts anyway — but a warning that cries wolf in every hourly email, which
#: is how a real signed-out warning ends up skimmed past.
CHURNING_COOKIES = frozenset({
    "KP_UIDz", "KP_UIDz-ssn", "ma.paramsToken", "SOTC", "ma.LANGUAGE",
})

#: Prefixes of cookies that are analytics whatever else is true of them.
#:
#: The signed-out baseline catches most of these, but not all: Google
#: Analytics mints a per-property cookie (_ga_MNQMF2C2CB) that only appears
#: once you have visited the pages behind a sign-in, so it looked like part of
#: the account. It carried a 2027 expiry, and reporting the longest-lived
#: "account" cookie therefore announced the session was good for 400 days.
#: That is exactly as wrong as the two hours it replaced.
ANALYTICS_PREFIXES = ("_ga", "_gid", "_gcl", "_fbp", "_uet", "_scid", "__gads",
                      "__gpi", "cto_", "_au_", "_pn_", "permutive", "_ddl")


def _stamp() -> str:
    """Local shim for state.stamp(), imported lazily to avoid a cycle."""
    from .state import stamp

    return stamp()


def _is_analytics(name: str) -> bool:
    return any(name.startswith(p) for p in ANALYTICS_PREFIXES)


#: Where the fingerprint taken at sign-in time is kept. Beside the profile
#: rather than inside it, so a Chrome profile reset cannot silently take the
#: evidence with it.
SESSION_FILE = config.BUY_PROFILE_DIR.parent / "buy-session.json"


def _chrome_time(microseconds: int) -> Optional[datetime]:
    """Chrome stores times as microseconds since 1601-01-01 UTC."""
    if not microseconds:
        return None
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=microseconds
        )
    except (OverflowError, ValueError):
        return None


def profile_cookies(profile_dir=None) -> dict:
    """{cookie_name: expiry_or_None} for ticketmaster.ie, read offline.

    Copies the database before reading it. Chrome holds a lock on the live
    file, and this has to work while a browser is open — the alternative is a
    check that only works when the thing being checked is shut, which is no
    check at all.
    """
    profile_dir = profile_dir or config.BUY_PROFILE_DIR
    db = profile_dir / "Default" / "Cookies"
    if not db.exists():
        return {}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copy(str(db), tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, expires_utc FROM cookies WHERE host_key LIKE ?",
            ("%ticketmaster%",),
        ).fetchall()
        conn.close()
        return {name: _chrome_time(exp) for name, exp in rows}
    except (sqlite3.Error, OSError):
        return {}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def anonymous_baseline() -> set:
    """Cookie names a signed-OUT profile on this machine actually carries.

    The watcher's own profile is the baseline, and it is a good one: it is
    real, it is current, and it is guaranteed signed out — staying signed out
    is the whole reason it exists. Anything present in it proves nothing about
    an account.

    Measured rather than assumed, because assuming was wrong. The hardcoded
    KNOWN_ANONYMOUS_COOKIES list was written from a single partial sample and
    missed fifteen ordinary names, so a profile that had never signed in was
    confidently reported as signed in.

    Returns an empty set if the watcher's profile cannot be read, in which
    case the hardcoded list is all there is — weaker, but never worse than
    before.
    """
    try:
        return set(profile_cookies(config.PROFILE_DIR))
    except Exception:
        return set()


def record_signed_in_fingerprint(profile_dir=None) -> dict:
    """Remember what this profile looked like at the moment of signing in.

    Called by `login-buy` once David confirms he is signed in. The cookies
    that are present now but were not on a signed-out profile are, by
    construction, the ones the account is carried in. Nobody has to guess
    their names.
    """
    cookies = profile_cookies(profile_dir)
    # Both baselines: the hardcoded list and the live signed-out profile. The
    # second is what stops fifteen ordinary anonymous cookies being recorded
    # as the account's, which would make every later check meaningless.
    # CHURNING_COOKIES are excluded here as well as at comparison time. Older
    # records already contain them — the one written on 2026-08-19 has five —
    # so the comparison has to filter regardless, but there is no reason to
    # keep writing them into new ones. See CHURNING_COOKIES for what they are
    # and why their absence means nothing.
    auth = sorted(n for n in (set(cookies) - KNOWN_ANONYMOUS_COOKIES
                              - anonymous_baseline() - CHURNING_COOKIES)
                  if not _is_analytics(n))

    # Split by whether they survive the browser closing.
    #
    # Two of the fourteen recorded on 2026-08-19 — TMAUO and ma.SID — carry no
    # expiry at all. Those are session cookies: Chrome drops them when it
    # exits, which it does after every securing attempt. Requiring all
    # fourteen therefore reported a perfectly good profile as signed out the
    # moment the browser had been used once. Only the persistent ones can
    # answer "is this profile still signed in" between runs.
    persistent = sorted(n for n in auth if cookies.get(n))
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "auth_cookies": auth,
        #: The subset the later checks actually compare against.
        "persistent_cookies": persistent,
        "cookie_count": len(cookies),
    }
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(record, indent=2))
    except OSError:
        pass
    return record


def session_evidence(profile_dir=None) -> dict:
    """What can be said about the buying session without opening a browser.

    Returns {signed_in, reason, expires_at, days_left}. `signed_in` is None —
    not False — when there is genuinely no way to tell, because "we cannot
    say" and "definitely signed out" call for different words and different
    actions.
    """
    profile_dir = profile_dir or config.BUY_PROFILE_DIR
    out = {"signed_in": None, "reason": "", "expires_at": None, "days_left": None}

    if not profile_dir.exists():
        out.update(signed_in=False, reason="no buying profile — login-buy has never run")
        return out

    cookies = profile_cookies(profile_dir)
    if not cookies:
        out.update(signed_in=False, reason="the profile holds no ticketmaster cookies")
        return out

    try:
        recorded = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        # No fingerprint: a profile that predates it, or one whose record was
        # lost. Compare against the WATCHER's profile instead of a hardcoded
        # list, because that profile is on this machine, is guaranteed signed
        # out, and is always current.
        #
        # The hardcoded list alone is not good enough and was caught being
        # wrong on 2026-08-19. It was written by hand from one partial sample,
        # so fifteen perfectly ordinary anonymous cookies — SID, TMUO,
        # eps_sid, tmp_id and the rest — were missing from it, and a profile
        # that had never signed in was reported as signed in. That is the
        # dangerous direction: doctor goes green, the banner warning
        # disappears, and the first anyone knows is a listing not being held.
        extra = sorted(set(cookies) - KNOWN_ANONYMOUS_COOKIES - anonymous_baseline())
        if extra:
            out.update(
                signed_in=True,
                reason=f"{len(extra)} cookie(s) that a signed-out profile on "
                       f"this machine does not have (no sign-in fingerprint "
                       f"recorded — re-run login-buy to make this exact)",
            )
        else:
            out.update(signed_in=False,
                       reason="every cookie here is one a signed-out profile "
                              "also has — not signed in")
        return out

    # Only the cookies that survive the browser closing. Session-scoped ones
    # are dropped by Chrome on exit — see record_signed_in_fingerprint — so
    # their absence says nothing about being signed out. Older records that
    # predate the split fall back to filtering the full list the same way.
    expected = set(recorded.get("persistent_cookies") or [])
    if not expected:
        expected = {n for n in (recorded.get("auth_cookies") or []) if cookies.get(n)}
    if not expected:
        out.update(reason="the recorded sign-in found no lasting account cookies "
                          "to watch — re-run the sign-in")
        return out

    # Judge on the cookies that MEAN something, not on the ones that churn.
    #
    # See CHURNING_COOKIES. The recorded fingerprint contains bot-detection
    # tokens and preferences alongside the account, because at sign-in they
    # were simply "present here and not on a signed-out profile". Requiring
    # all of them to survive means a browser-identity refresh — which this
    # watcher performs every 90 minutes by design — reports the account as
    # signed out.
    stable = expected - CHURNING_COOKIES
    if not stable:
        # Everything recorded was a churner. Nothing here can answer the
        # question, and None is the honest verdict: "cannot tell" and
        # "signed out" call for different words and different actions.
        out.update(
            reason="the sign-in fingerprint holds only short-lived cookies, "
                   "which cannot say whether the account is still signed in — "
                   "re-run the sign-in to record a better one",
        )
        return out

    missing = sorted(stable - set(cookies))
    if missing:
        out.update(
            signed_in=False,
            reason=f"the account cookie(s) recorded at sign-in are gone "
                   f"({', '.join(missing[:3])}) — it has been signed out",
        )
        return out

    # When the first account cookie lapses.
    #
    # Not "when the session expires", because nothing here can know that —
    # only Ticketmaster does, and it can invalidate a session server-side at
    # any time regardless of what the cookies say. Two attempts at a
    # confident number both misled on 2026-08-19: the soonest expiry picked
    # SOTC and announced "0.1 days" on a healthy profile, and the longest
    # picked a Google Analytics cookie and announced 400 days.
    #
    # So this reports the earliest lapse among real account cookies, which is
    # the first moment anything is known to change, and the callers word it as
    # that rather than as a guarantee.
    # PRESENCE is the verdict. Expiry is information, and only information.
    #
    # This used to flip signed_in to False the moment the earliest-expiring
    # recorded cookie passed its date, and at 21:00 on 2026-08-19 that
    # declared a perfectly good session dead. The eleven cookies recorded at
    # sign-in are not one kind of thing: `id-token` is the account and had 29
    # days left, while SOTC, KP_UIDz-ssn and ma.paramsToken are short-lived
    # operational cookies the site reissues on the next page load. Judging the
    # session by the soonest of those is judging it by the part designed to
    # churn.
    #
    # It failed in the dangerous direction. doctor, check-buy and the startup
    # banner all went red, and the fix they printed was to sign in again —
    # which for an account already signed in means putting a password through
    # a scripted login for no reason, against the very account the
    # two-browser design exists to keep away from attention.
    #
    # A cookie still IN the profile has not been dropped by Chrome. If the
    # account cookies genuinely go, the `missing` check above catches it, and
    # that is the check that carries meaning.
    now = datetime.now(timezone.utc)
    churned = sorted((expected & CHURNING_COOKIES) - set(cookies))
    if churned:
        # Worth saying, and worth saying as normal. Somebody comparing this
        # output against the recorded fingerprint by hand will notice the
        # difference and should not have to wonder whether it matters.
        out.update(
            signed_in=True,
            reason=f"the account cookies are present; "
                   f"{', '.join(churned[:3])} rotated away, which is what "
                   f"those do and says nothing about the account",
        )
    else:
        out.update(signed_in=True,
                   reason="the account cookies recorded at sign-in are all present")

    # The next real change is the soonest expiry still in the FUTURE. A date
    # already passed on a cookie that is nonetheless present describes one
    # mid-reissue, not a session ending.
    future = [cookies[n] for n in stable if cookies.get(n) and cookies[n] > now]
    soonest = min(future) if future else None
    if soonest:
        # Four decimals, not one: rounding days to 1dp collapsed everything
        # under about 72 minutes to 0.0, and describe_lapse read 0.0 as
        # "already" — reporting a cookie 54 minutes from lapsing as gone.
        left = (soonest - now).total_seconds() / 86400.0
        out.update(expires_at=soonest.isoformat(), days_left=round(left, 4))
    else:
        out.update(reason="the account cookies recorded at sign-in are all "
                          "present, though every recorded expiry has passed — "
                          "they are being reissued, which is normal")
    return out


def profile_in_use(profile_dir=None) -> bool:
    """Is a Chrome already running on this profile directory?

    Chrome takes an exclusive lock on a user-data-dir, and the buying browser
    is deliberately LEFT OPEN after a successful hold, because closing it is
    what drops the basket. Those two facts collide on the second find of a
    busy afternoon: the first hold's window is still up, the profile is
    locked, and Playwright fails with a message about a singleton lock that
    says nothing about a ticket.

    Six real listings appeared on 2026-08-18, and eight sightings fell inside
    one day, so two finds inside a fifteen-minute hold window is an ordinary
    Tuesday rather than a corner case.

    Asked of the process table rather than of Chrome's own SingletonLock file
    in the profile, which survives a crash and would report the profile busy
    forever afterwards. Any error answers False: this gate must never be the
    reason a real listing goes unheld, so when it cannot tell, the attempt
    goes ahead and fails honestly on its own terms.
    """
    from pathlib import Path

    profile_dir = Path(profile_dir or config.BUY_PROFILE_DIR)

    # Anchored on the end of the argument, and that anchor is the whole
    # correctness of this function. `pgrep -f` matches a substring of the
    # command line, so an unanchored "user-data-dir=.../chrome-profile" also
    # matches ".../chrome-profile-buy" — asking whether the WATCHER's profile
    # was busy would have answered yes whenever the buying browser was open,
    # and the poll loop would have reset a perfectly good profile. It is the
    # same mistake restart.sh made in the other direction, found the same way,
    # and caught here only because the test for this function asked about two
    # profiles sharing a prefix.
    #
    # The path is escaped because it is a regular expression to pgrep, and a
    # real one contains a dot: ".ep2026-watcher" would otherwise match
    # "Xep2026-watcher" too.
    specials = ".[]()*+?{}|^$\\"
    quoted = "".join("\\" + ch if ch in specials else ch for ch in str(profile_dir))
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={quoted}( |$)"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return found.returncode == 0 and bool(found.stdout.strip())


def release_buying_browser(profile_dir=None) -> bool:
    """Close the buying browser, dropping whatever it is holding. True if one died.

    Only ever called to make room for a HIGHER priority ticket — see
    Event.secure_priority. It throws away a live basket, which is normally the
    worst thing this codebase can do, and is right in exactly one case: an
    Early Entry pass is being held and a Weekend Ticket has appeared. The pass
    is only valid alongside a weekend ticket, so holding it while the ticket
    goes by spends the one buying browser on the one product that is useless
    on its own.

    Done by killing the process rather than by calling close() on the session,
    and that is not laziness. The session was created inside the securing
    thread, and Playwright's sync objects belong to the thread that made them;
    closing one from another thread fails in the same family of ways that made
    the threading necessary in the first place. The process table does not
    care which thread is asking.

    The pattern is anchored on the end of the argument for the same reason
    restart.sh's is: `pkill -f` matches substrings, and an unanchored buy
    profile path would also match a longer one.
    """
    from pathlib import Path

    profile_dir = Path(profile_dir or config.BUY_PROFILE_DIR)
    specials = ".[]()*+?{}|^$\\"
    quoted = "".join("\\" + ch if ch in specials else ch for ch in str(profile_dir))
    try:
        killed = subprocess.run(
            ["pkill", "-f", f"user-data-dir={quoted}( |$)"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # Chrome needs a moment to let go of the profile lock, or the browser we
    # are about to open for the weekend ticket fails on the way in.
    if killed.returncode == 0:
        time.sleep(2.0)
        return True
    return False


@dataclass
class HoldResult:
    """What came of trying to secure one listing."""

    #: True only when a basket was positively confirmed on the page. Never
    #: inferred from the absence of an error — a hold nobody can see is worse
    #: than no hold, because it sends David to a screen with nothing on it.
    secured: bool = False
    #: Why not, in words fit for an alert. Empty when secured.
    reason: str = ""
    notes: List[str] = field(default_factory=list)
    #: How long he has, for wording only. Read off the checkout page's own
    #: countdown when one is visible, otherwise config.HOLD_MINUTES_HINT.
    minutes_hint: int = 0
    #: Where the checkout is, captured the moment a basket is confirmed.
    #: Offered to David's phone as worth trying — see the note at the capture
    #: site. Empty when nothing was held.
    checkout_url: str = ""
    #: True when an earlier, lower-priority hold was dropped to attempt this
    #: one. The alerts say so either way: he needs to know the Early Entry
    #: pass was let go, whether or not the weekend ticket was then caught.
    preempted: bool = False
    #: True when minutes_hint was read from the page rather than estimated.
    #: The alert says which, because "you have about ten minutes" and "the
    #: page says 11:39" deserve different amounts of trust — and the estimate
    #: comes from one observation of an entirely different event.
    minutes_measured: bool = False

    #: Seconds spent on each step, in the order they happened.
    #:
    #: Added 2026-08-20, because the race was being tuned on inference. Two
    #: weekend listings at €366.39 were found and lost that day, and the best
    #: anyone could say about why was "roughly sixty seconds, probably" —
    #: derived from minute-resolution log lines. That is not a measurement,
    #: and you cannot optimise against it.
    #:
    #: Now every failed hold says exactly where its seconds went, which turns
    #: the next lost ticket from a shrug into a number. It also settles
    #: whether keeping the buying browser warm was worth it, rather than
    #: leaving that as an opinion.
    timings: "OrderedDict[str, float]" = field(default_factory=OrderedDict)
    #: When the attempt began, for total elapsed.
    started_at: float = field(default_factory=time.monotonic)
    #: The clock the next mark() measures from.
    _last_mark: float = field(default_factory=time.monotonic)

    # ── Forensics for a lost race ────────────────────────────────────────────
    #
    # Added 2026-08-20 after two weekend listings were found and lost within
    # half an hour, both with a complete and fast pipeline: the row was located
    # in 0.0s and the click landed on "sold or removed from sale". The timings
    # said the attempt took 14 and 17 seconds, which sounds like a speed
    # problem — but nothing in the record could distinguish the two
    # explanations, and they call for opposite responses:
    #
    #   * SOLD. Somebody genuinely bought it in those seconds. The answer is to
    #     be faster, and every second is worth chasing.
    #   * HELD, or never purchasable. The listing is in another buyer's basket,
    #     or the feed is advertising something the offer flow will not honour.
    #     Then being faster wins nothing at all, because there is nothing to
    #     win — and the answer is to WAIT and re-attempt, since baskets expire.
    #
    # The endpoint can tell them apart, and it is one call. If the listing is
    # still in the feed immediately after Ticketmaster has said it is gone,
    # then "sold" is not what happened.
    #
    #: Was the listing still in the resale feed at the moment of failure?
    #: True/False, or None when the endpoint could not be asked.
    still_listed_after: Optional[bool] = None
    #: The listing ids the feed returned at that moment, for comparison with
    #: the id the find was reported under. These ids have been observed to
    #: change between polls for what is plainly the same listing, so an id that
    #: differs is evidence about the feed rather than about the ticket.
    ids_after: List[str] = field(default_factory=list)
    #: The id this attempt set out to secure.
    listing_id: str = ""
    #: Where the click actually landed. Captured on failure as well as on
    #: success, because the URL of the dead end is the only place the direct
    #: link to a listing has ever been visible — and a direct link is what
    #: would let a future attempt skip the search entirely.
    landed_url: str = ""
    #: Seconds between the listing being SEEN and this attempt starting. The
    #: step timings only measure the attempt; the sweep that found it may have
    #: been up to its whole interval behind, and that latency is invisible in a
    #: report that starts its clock when the buyer wakes up.
    detected_age: Optional[float] = None
    #: How many goes it took. More than one only ever happens when the feed
    #: said the ticket had not really sold — see secure().
    attempts: int = 1

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"    [buyer] {text}")

    def mark(self, step: str) -> float:
        """Record the seconds spent since the previous mark. Returns them.

        Deliberately measures the GAP rather than the total, so the steps sum
        to the elapsed time and the slow one is obvious at a glance. Repeated
        step names accumulate rather than overwrite — a retry is still time
        spent on that step, and hiding it would flatter exactly the step that
        needs looking at.
        """
        now = time.monotonic()
        spent = now - self._last_mark
        self._last_mark = now
        self.timings[step] = self.timings.get(step, 0.0) + spent
        return spent

    @property
    def elapsed(self) -> float:
        """Total seconds from the start of the attempt to now."""
        return time.monotonic() - self.started_at

    def timing_line(self) -> str:
        """One line for the log and the email, slowest step called out.

        Empty when nothing was measured, so callers can drop it rather than
        print a heading over nothing.
        """
        if not self.timings:
            return ""
        parts = " ".join(f"{k} {v:.1f}s" for k, v in self.timings.items())
        slowest = max(self.timings.items(), key=lambda kv: kv[1])
        return (f"{parts} | total {sum(self.timings.values()):.1f}s "
                f"| slowest: {slowest[0]} at {slowest[1]:.1f}s")


class BuySession:
    """A signed-in Chrome, opened on a find and held while a basket is live.

    Deliberately not a long-lived singleton like the watcher's session. It
    exists for the length of one attempt plus however long David needs to pay,
    and closing it is what releases the hold — so it is closed by the caller,
    explicitly, never by a timeout in here.
    """

    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir or config.BUY_PROFILE_DIR
        self._session = None

    def start(self):
        # Imported here, not at module scope, so that importing this module —
        # which the tests and the alerting path both do — never costs a
        # Playwright import or requires it to be installed.
        from .sources.browser import BrowserSession

        # Headed and ON SCREEN, both load-bearing. Headless gets 403 from
        # Ticketmaster, and offscreen would park the window at -2400 where he
        # cannot finish paying in it — which is the entire point of the
        # session existing.
        was_offscreen = config.OFFSCREEN
        config.OFFSCREEN = False
        try:
            self._session = BrowserSession(headless=False, profile_dir=self.profile_dir)
            self._session.start()
        finally:
            config.OFFSCREEN = was_offscreen
        return self

    def close(self):
        """Closing releases the basket. Only the caller decides when."""
        if self._session is not None:
            self._session.close()
            self._session = None

    @property
    def page(self):
        return self._session.page

    def await_listings(self, result: "HoldResult", budget_s: float) -> bool:
        """Wait until the resale panel can actually be read. True if it can.

        The reason the first three real securing attempts all failed.
        
        Pressing search does not produce listings. The search resolves, and
        only THEN does a separate call to /api/quickpicks/{event}/resale come
        back and render "Other Options → Verified Resale Tickets" — a fact the
        watcher's own module establishes at length, because reading the page
        too early is what once recorded a quarter of its polls as resale-blind.

        The buyer ignored all of that. It clicked search and looked for the
        listing row five seconds later, which on the watcher's own measurements
        is well before the panel exists. So it reported "the listing was gone
        from the page by the time the buying browser reached it" on 2026-08-19
        at 17:58, 19:05 and 19:12 — three real listings, each almost certainly
        still sitting there, each recorded as sold.

        That verdict was worse than the failure. It read as losing a race,
        which invites making the watcher faster; the actual fault was looking
        before the page had drawn anything, which no amount of speed fixes.

        Reuses the watcher's own two waits rather than reimplementing them.
        They encode a fortnight of findings about when this page is readable,
        and a second copy would drift from the first.
        """
        session = self._session
        deadline = time.monotonic() + budget_s

        # Two thirds of what is left for the search to come back, because a
        # search that has not resolved cannot have a panel under it.
        outcome = session._await_result(timeout_s=max(5.0, (deadline - time.monotonic()) * 0.66))
        if outcome == "basket":
            result.note("the search went straight to a basket")
            return True
        if outcome == "timeout":
            result.note("the search did not resolve in time — the page is slow, "
                        "not necessarily empty")
            return False
        result.note("search resolved")

        left = deadline - time.monotonic()
        if left <= 0:
            return False
        readable, why = session._await_resale_panel(
            timeout_s=max(5.0, left),
            render_s=min(8.0, max(2.0, left / 3.0)),
        )
        result.note(f"resale panel: {'readable' if readable else why}")
        return readable

    def listings_now(self, event, qty: int):
        """Ask the resale endpoint what is actually on offer, right now.

        Used to tell "the listing has sold" apart from "the page has not drawn
        it yet" — which the row hunt alone cannot do, and which decides whether
        a failure means the race was lost or the code looked too early.
        """
        return self._session.fetch_resale_json(event, qty)

    def set_quantity(self, qty: int, result: "HoldResult") -> None:
        """Drive the page's quantity stepper, reusing the watcher's logic.

        The stepper is a role=spinbutton driven with arrow keys because the
        page floats an overlay over it that eats real clicks — knowledge that
        cost a day to find and must not be duplicated here and left to drift.
        """
        self._session._set_quantity(qty, _NoteSink(result))

    # There is deliberately no `signed_in()` method here.
    #
    # There was one, and it asked `"sign out" in page_text or "my account" in
    # page_text` — the exact test the module header above shows was checked
    # against every page capture the watcher has ever taken and found in none
    # of them. Ticketmaster renders the account control somewhere Playwright's
    # flattened inner_text cannot reach, so it answered "signed out" for a
    # perfectly good session.
    #
    # By the time that was established the method had no callers, which made
    # it more dangerous rather than less: dead code that looks like the
    # obvious answer is what the next person reaches for. Ask
    # session_evidence() instead — it reads the cookie database, and it is
    # what secure(), doctor and check-buy all already use.


class _ParkNotes:
    """Swallow the note() calls set_quantity makes while nobody is listening.

    _NoteSink writes into a HoldResult, and there is no attempt in progress
    when the browser is merely parking. Printing them would put "quantity set
    to 1" into the log after every find — noise that reads like something
    happening.
    """

    def note(self, text: str) -> None:
        pass


class BuyerWorker(threading.Thread):
    """A signed-in browser kept open and warm, waiting for a listing.

    Measured on 2026-08-20: two weekend listings at €366.39 were found and
    lost, and once the detection lag was fixed the entire remaining gap was
    this — about sixty seconds between seeing a listing and clicking its row,
    spent almost wholly on work that could have been done in advance. A fresh
    Chrome was launched, the event page loaded through its 401-then-reload
    dance, and only then did the part that depends on the listing begin. These
    listings are consumed in well under a minute.

    So the browser is opened once, at startup, and parked on the event page.
    When a listing appears the attempt begins at the search.

    THE THREAD IS THE POINT, not an optimisation. Playwright's sync objects
    belong to the thread that created them, which is why securing already ran
    in a thread of its own — see secure_in_thread. A browser created at
    startup and then driven from a different thread fails in exactly the
    family of ways that made the threading necessary in the first place.

    That constraint shapes the whole class: ONE thread owns the session for
    its entire life, and every operation that touches the page — attempting,
    re-parking, dropping a basket to make room, shutting down — is a job on a
    queue rather than a method that reaches in from outside. The public
    methods only ever put things on the queue and read a state string.

    Everything degrades. If the worker cannot start, has died, or is busy,
    secure_in_thread falls back to the cold-start path that has always been
    there. A warm browser is a speed-up, never a dependency.
    """

    #: Re-load the parked page this often, so a browser left open for hours is
    #: not asked to act on a page rendered before lunch. One page load per
    #: interval, spent while nothing is happening rather than during a race.
    REFRESH_MINUTES = float(os.environ.get("EP_BUY_WARM_REFRESH", "20"))

    #: How long to wait at startup for the browser to come up. Generous: it
    #: happens once, alongside the watcher's own browser starting.
    STARTUP_TIMEOUT = float(os.environ.get("EP_BUY_WARM_STARTUP", "120"))

    def __init__(self, home=None):
        super().__init__(name="ep-buyer-warm", daemon=True)
        #: The page to sit on while idle — the most important securable one,
        #: so the commonest find needs no navigation at all.
        self.home = home or next(
            (e for e in config.EVENTS if e.secure), config.EVENTS[0])
        self._jobs = queue.Queue()
        self._session = None
        self._parked_on = None
        self._lock = threading.Lock()
        #: "starting" -> "idle" <-> "busy" -> "holding" -> "idle" | "dead"
        self._state = "starting"
        self._ready = threading.Event()
        self._stop = threading.Event()

    # ── state, safe to read from any thread ──────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set(self, state: str) -> None:
        with self._lock:
            self._state = state

    @property
    def available(self) -> bool:
        """Idle and alive, so it can take a job now."""
        return self.state == "idle" and self.is_alive()

    @property
    def holding(self) -> bool:
        """Is a basket live in this browser?

        This is what replaces profile_in_use() once a browser is kept warm.
        That check greps the process table for a Chrome on the buying profile,
        which was a fair proxy while the browser existed only during an
        attempt — and becomes permanently true the moment one is warm, which
        would refuse every job for ever. Asking the worker is both cheaper and
        actually correct.
        """
        return self.state == "holding"

    # ── the thread: the only place the page is ever touched ──────────────

    def run(self) -> None:
        try:
            self._session = BuySession().start()
            self._park(self.home, force=True)
            self._set("idle")
        except Exception as exc:
            print(f"[{_stamp()}] warm buying browser could not start: "
                  f"{type(exc).__name__}: {exc} — cold starts will be used")
            self._set("dead")
            self._ready.set()
            return
        finally:
            self._ready.set()

        last_refresh = time.monotonic()
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=5.0)
            except queue.Empty:
                # Keep the parked page fresh, but ONLY while genuinely idle.
                # A reload while holding is precisely what throws the basket
                # away, which is the most expensive thing this class can do.
                if (self.state == "idle"
                        and time.monotonic() - last_refresh > self.REFRESH_MINUTES * 60):
                    self._safe_park()
                    last_refresh = time.monotonic()
                continue

            kind = job[0]
            if kind == "stop":
                break
            if kind == "release":
                # The hold is over — David paid, or it lapsed. Navigating away
                # is what actually drops whatever is left in the basket.
                if self.state == "holding":
                    self._safe_park()
                    self._set("idle")
                continue
            if kind == "secure":
                self._run_job(job)
                last_refresh = time.monotonic()

        if self.state != "holding" and self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def _park(self, event, force: bool = False) -> None:
        if self._session is None:
            return
        if not force and self._parked_on == event.slug:
            return
        self._session.page.goto(event.url, wait_until="domcontentloaded")
        self._parked_on = event.slug
        self._prearm()

    def _prearm(self) -> None:
        """Set the quantity now, while nothing is waiting on it.

        The page loads with a quantity of 2 and resale results are filtered by
        quantity, so every attempt has to set it to 1 before it can search.
        Doing that on the critical path cost 1.6s and 6.5s on the two attempts
        of 2026-08-20 — the variation is the stepper rendering after a fresh
        page load, which is exactly the work that does not need a listing to
        be happening.

        Set here instead, on the parked page, where the seconds are free. The
        attempt still calls set_quantity, which now finds the value already
        correct and returns on its fast path rather than driving the stepper.

        Costs no request: the stepper is a client-side control and nothing is
        submitted until search is pressed.

        Never raises. A page that will not take a quantity here is a page the
        attempt will have to deal with itself, and a parked browser failing
        loudly over a preparation step would take out the warm browser for a
        problem that has not happened yet.
        """
        try:
            self._session.set_quantity(config.WANTED_QUANTITY, _ParkNotes())
        except Exception as exc:
            print(f"[{_stamp()}] warm browser could not pre-set the quantity: "
                  f"{type(exc).__name__}: {exc}")

    def _safe_park(self) -> None:
        """Re-park, swallowing anything. Used off the critical path only."""
        was = self.state
        try:
            self._set("busy")
            self._park(self.home, force=True)
        except Exception as exc:
            print(f"[{_stamp()}] warm browser could not re-park: {exc}")
        finally:
            self._set("idle" if was != "dead" else "dead")

    def _run_job(self, job) -> None:
        _, event, listing, may_preempt, box, done = job
        result = box["result"]
        try:
            if self.state == "holding":
                # Only reachable when the caller granted preemption; submit()
                # refuses otherwise. Navigating away drops the basket, which
                # is the whole meaning of preempting.
                result.note("dropping the live hold to go for a more "
                            "important ticket")
                self._park(self.home, force=True)
                result.preempted = True
            self._set("busy")
            if self._parked_on == event.slug:
                result.note("buying browser was already warm on this page")
                result.mark("warm")
            else:
                self._park(event)
                result.mark("navigate")
            box["result"] = secure(self._session, event, listing, result)
        except Exception as exc:
            result.reason = f"{type(exc).__name__}: {exc}"
            result.note(f"warm secure attempt failed: {result.reason}")
            box["result"] = result
        finally:
            if box["result"].secured:
                # The browser IS the checkout window now. Nothing may reuse,
                # reload or close it until the hold is done with.
                self._set("holding")
            else:
                self._safe_park()
            done.set()

    # ── the API, which only ever enqueues ────────────────────────────────

    def wait_until_ready(self, timeout: float = None) -> bool:
        self._ready.wait(self.STARTUP_TIMEOUT if timeout is None else timeout)
        return self.available

    def submit(self, event, listing, result, timeout_s: float, may_preempt=False):
        """Hand one attempt over. HoldResult, or None meaning 'not mine'.

        None is an instruction to the caller: fall back to a cold start. A
        definite refusal comes back as a HoldResult with a reason, because
        "the browser is holding something more important" is an answer David
        needs to read, not a reason to open a second browser on the same
        profile while a basket is live.
        """
        state = self.state
        if state in ("dead", "starting") or not self.is_alive():
            return None
        if state == "busy":
            return None
        if state == "holding" and not may_preempt:
            result.reason = (
                "the buying browser is already open holding something at "
                "least as important as this, so it was left alone. Finish "
                "or close that window and this page can be secured by hand; "
                "nothing was touched here."
            )
            result.note(result.reason)
            return result

        box = {"result": result}
        done = threading.Event()
        self._jobs.put(("secure", event, listing, may_preempt, box, done))
        if not done.wait(timeout_s):
            result.reason = (
                f"the warm buying browser was still working after "
                f"{timeout_s:.0f}s and was abandoned — the poll loop must "
                f"not wait on it"
            )
            return result
        return box["result"]

    def release(self) -> None:
        """The hold is finished with. Ask the worker to free the browser."""
        if self.state == "holding":
            self._jobs.put(("release",))

    def shutdown(self) -> None:
        self._stop.set()
        self._jobs.put(("stop",))


def secure_in_thread(event, listing, timeout_s: int = None,
                     may_preempt: bool = False, worker=None) -> HoldResult:
    """Open the buying browser and hold `listing`, from its own thread.

    The thread is not an optimisation, it is the only way this works.
    Playwright's sync API refuses to start a second instance in a thread that
    already has an asyncio loop running, and the watcher's own browser has one
    running for the whole life of the process. Every securing attempt on
    2026-08-19 therefore died before it opened anything:

        Error: It looks like you are using Playwright Sync API inside the
        asyncio loop. Please use the Async API instead.

    Three real listings were found that afternoon — two Early Entry passes at
    €46.50 and a Weekend Camping at €366.39 — and all three produced a
    perfect availability alert followed by that message. The watcher was never
    going to hold anything, and no offline test could have caught it, because
    the fault only exists when a second Playwright starts inside a live one.

    A fresh thread has no event loop of its own, so sync_playwright() starts
    cleanly there. The thread is given a hard deadline and is left to die on
    its own if it overruns: a hung browser must not wedge the poll loop, which
    is the one thing that must keep running whatever else breaks.
    """
    from .state import stamp as state_stamp

    budget = timeout_s or (config.SECURE_TIMEOUT_SECONDS + 60)

    # The warm path, when there is one. See BuyerWorker: the browser is
    # already open and already on the page, so the attempt starts at the
    # search rather than at a cold Chrome launch and a 401-reload dance —
    # which together were most of the sixty seconds these listings do not
    # give us.
    #
    # A None back means "not mine, use the cold path": dead, still starting,
    # or busy with another attempt. A HoldResult back is a real answer,
    # including a refusal, and must be returned rather than retried cold —
    # opening a second browser on the same profile while a basket is live is
    # how a caught ticket gets thrown away.
    if worker is not None:
        warm = worker.submit(event, listing, HoldResult(), budget,
                             may_preempt=may_preempt)
        if warm is not None:
            line = warm.timing_line()
            if line:
                print(f"[{state_stamp()}] hold timings (warm): {line}")
            return warm

    box = {"result": HoldResult()}

    def run():
        session, hold = None, HoldResult()
        # Before opening anything. A buying browser that is already up is
        # almost always the previous hold still waiting to be paid for, and
        # the right answer is to say so — not to fail on a profile lock, and
        # certainly not to close the old window, which would drop a ticket
        # that is already caught in order to chase one that is not.
        if profile_in_use():
            if not may_preempt:
                hold.reason = (
                    "the buying browser is already open holding something at "
                    "least as important as this, so it was left alone. Finish "
                    "or close that window and this page can be secured by "
                    "hand; nothing was touched here."
                )
                hold.note(hold.reason)
                box["result"] = hold
                return
            # Outranks what is being held. Let go of it and take this instead.
            hold.note("a more important ticket than the one being held — "
                      "releasing the buying browser to go for this one")
            if release_buying_browser():
                hold.preempted = True
                hold.note("the earlier hold has been dropped")
        try:
            session = BuySession().start()
            hold.mark("launch")
            hold = secure(session, event, listing, hold)
        except Exception as exc:
            hold.reason = f"{type(exc).__name__}: {exc}"
            hold.note(f"secure attempt failed to start: {hold.reason}")
        finally:
            # Left OPEN on success: closing the browser is what drops the
            # basket, and the whole point is that David walks to this window
            # and pays in it. Closed on failure, because a signed-in Chrome
            # nobody is going to use is just an idle session to fingerprint.
            if session is not None and not hold.secured:
                try:
                    session.close()
                except Exception:
                    pass
            # Where the seconds went, win or lose. On a loss this is the whole
            # diagnosis: these listings are consumed in well under a minute,
            # so whichever step ate the most of it is the only thing worth
            # arguing about afterwards.
            line = hold.timing_line()
            if line:
                print(f"[{state_stamp()}] hold timings: {line}")
            box["result"] = hold

    worker = threading.Thread(target=run, name="ep-secure", daemon=True)
    worker.start()
    worker.join(timeout=budget)
    if worker.is_alive():
        box["result"].reason = (
            f"the securing browser was still working after {budget}s and was "
            f"abandoned — the poll loop must not wait on it"
        )
    return box["result"]


def secure(session: BuySession, event, listing, result: HoldResult = None) -> HoldResult:
    """Put `listing` in a basket, and try again if it was never takeable.

    One attempt is _secure_once below. This adds the only retry that is worth
    making, and refuses the one that is not.

    The distinction comes from the probe on the dead-end screen. When
    Ticketmaster says "sold or removed" and its own resale feed AGREES the
    ticket is gone, it sold: going back is pointless and would only spend
    requests against a rate limit that has already blocked this connection.
    That case returns immediately, exactly as before.

    But when the feed still lists the ticket a second after refusing it,
    nothing was sold. Something is holding it — most likely another buyer's
    basket — and those lapse. That is the case worth waiting out, and it is
    the case a faster watcher could never have won, because there was nothing
    to win at the moment it looked.

    Bounded by the same SECURE_TIMEOUT_SECONDS budget the single attempt
    always had, so this cannot hold the buying browser — or the poll loop
    behind it — any longer than it could before. A weekend ticket can still
    preempt the whole thing.
    """
    result = result or HoldResult()
    deadline = time.monotonic() + config.SECURE_TIMEOUT_SECONDS

    for attempt in range(1 + config.SECURE_RETRIES):
        if attempt:
            result.attempts = attempt + 1
            result.note(f"attempt {attempt + 1}: going back for it")
        out = _secure_once(session, event, listing, result, deadline)
        if out.secured:
            return out
        # Genuinely sold, or the question could not be asked. Either way there
        # is nothing to come back for.
        if not out.still_listed_after:
            return out
        if attempt >= config.SECURE_RETRIES:
            out.note(f"still listed, but {attempt + 1} attempts is the limit — "
                     f"the alert tells David to try it himself")
            return out
        pause = config.SECURE_RETRY_PAUSE_SECONDS
        if time.monotonic() + pause >= deadline:
            out.note("still listed, but there is no time left in the window "
                     "to go back — the alert tells David to try it himself")
            return out
        out.note(f"it is still in the feed, so it did not sell — waiting "
                 f"{pause:.0f}s for the basket holding it to lapse")
        # Cleared so the next attempt's probe answers for itself. A stale True
        # here would be read as evidence from a look that never happened.
        out.still_listed_after = None
        out.ids_after = []
        time.sleep(pause)
    return out


def _secure_once(session: BuySession, event, listing,
                 result: HoldResult = None, deadline: float = None) -> HoldResult:
    """One attempt at putting `listing` in a basket. Returns without paying, always.

    `session` must already be started and signed in. Failure at any step is
    recorded and returned rather than raised: the caller's next move is to
    send the ordinary "a ticket is live" alert, which must not be lost
    because this optimistic extra step went wrong.

    `deadline` is a monotonic clock shared with secure() above, so that
    retries spend one budget between them rather than a fresh one each.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    from .sources.browser import BASKET_MARKERS, SEARCH_BUTTONS

    result = result or HoldResult()
    if deadline is None:
        deadline = time.monotonic() + config.SECURE_TIMEOUT_SECONDS

    def out_of_time() -> bool:
        if time.monotonic() < deadline:
            return False
        result.reason = (
            f"gave up after {config.SECURE_TIMEOUT_SECONDS}s — the listing is "
            f"most likely in someone else's basket"
        )
        result.note(result.reason)
        return True

    try:
        # Navigate BEFORE asking whether we are signed in. A freshly started
        # BrowserSession is parked on about:blank, which contains neither
        # "sign out" nor "my account" — so checking first meant the answer was
        # always "not signed in", and the whole feature was a no-op that would
        # have reported a login problem on the first real listing. Caught by
        # reading the flow back on 2026-08-19, before any listing tested it.
        page = session.page
        # Don't reload a page we are already standing on.
        #
        # The warm browser parks on the event page precisely so an attempt can
        # start at the search, and it reported "already warm on this page" on
        # both of the attempts on 2026-08-20 — and then spent a second
        # reloading that same page anyway, because this goto was
        # unconditional. The warm path saved the cold start and nothing else.
        #
        # Compared on the path alone. The parked URL can carry query
        # parameters the event URL does not, and a string mismatch here would
        # silently restore the reload while still reporting "warm".
        here = ""
        try:
            here = (page.url or "").split("?")[0].rstrip("/")
        except Exception:
            here = ""
        if here and here == event.url.split("?")[0].rstrip("/"):
            result.note(f"already on {event.slug} — starting at the search")
        else:
            page.goto(event.url, wait_until="domcontentloaded")
            result.note(f"opened {event.slug} in the buying browser")
        result.mark("navigate")

        # Note it, do not refuse on it.
        #
        # This used to return here when the session did not look signed in.
        # That gate was removed on 2026-08-19 once the detection behind it was
        # shown to be unreliable in the dangerous direction: Ticketmaster
        # renders no account text that Playwright's flattened inner_text can
        # read, so a perfectly good session reads as signed out. Refusing on
        # it would have thrown away the first real listing after David signed
        # in correctly.
        #
        # Trying anyway costs nothing that matters. A signed-out attempt
        # bounces off a login wall, holds nothing, and reports honestly — the
        # same outcome as refusing, minus the chance of being wrong about it.
        # The availability alert has already gone out either way.
        evidence = session_evidence()
        if evidence["signed_in"] is False:
            result.note(f"the buying session looks signed out ({evidence['reason']}) "
                        f"— trying anyway, since that reading can be wrong")
        elif evidence["signed_in"] is None:
            result.note("cannot tell whether the buying session is signed in — trying")

        # Same quantity discipline as the watcher: the page defaults to 2 and
        # resale results are filtered by quantity, so asking for the wrong
        # number manufactures a refusal against a listing that is really there.
        session.set_quantity(config.WANTED_QUANTITY, result)
        result.mark("quantity")
        try:
            page.get_by_role("button", name=SEARCH_BUTTONS).first.click(timeout=15_000)
            result.mark("search")
            result.note(f"searched for {config.WANTED_QUANTITY}")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            result.reason = f"could not press search in the buying browser: {exc}"
            result.note(result.reason)
            return result

        if out_of_time():
            return result

        # Wait for the panel before looking for anything in it. Pressing
        # search does not produce listings — a separate call has to answer and
        # the panel has to paint. Skipping this is why the first three real
        # attempts all reported the listing as gone. See await_listings().
        session.await_listings(result, budget_s=max(5.0, deadline - time.monotonic()))
        result.mark("panel")

        if out_of_time():
            return result

        # Find the listing row. Matched on the section rather than on the
        # listing id, because the id is an API field and has never been seen
        # in the rendered page — and section plus price is what distinguishes
        # one row from another when several are live.
        row = _find_listing_row(page, listing, result)
        result.mark("find_row")
        if row is None:
            # Do not guess at why. "Gone" and "not drawn" call for opposite
            # responses — one means the race was lost and nothing can be done,
            # the other means this code looked too early and is fixable — and
            # for three attempts they were reported identically, as the former.
            # The endpoint that the panel is a drawing of can tell them apart.
            still_there = None
            try:
                record = session.listings_now(event, config.WANTED_QUANTITY)
                data = (record or {}).get("data")
                if isinstance(data, dict):
                    picks = data.get("picks") or data.get("listings") or []
                    still_there = len(picks)
            except Exception:
                still_there = None

            if still_there:
                result.reason = (
                    f"the resale endpoint still shows {still_there} listing(s), "
                    f"but no row for them could be found on the page. That is a "
                    f"rendering or selector problem in the buying browser, not a "
                    f"lost race — the ticket was there and reachable by hand."
                )
            elif still_there == 0:
                result.reason = (
                    "the listing had genuinely sold — the resale endpoint "
                    "reports nothing left. The race was lost at the last step."
                )
            else:
                result.reason = (
                    "the listing could not be found on the page, and the resale "
                    "endpoint could not be asked either, so whether it sold or "
                    "simply never rendered is unknown"
                )
            result.note(result.reason)
            return result

        try:
            row.click(timeout=10_000)
            result.mark("click")
            result.note("clicked into the listing")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            result.reason = f"could not click the listing: {exc}"
            result.note(result.reason)
            return result

        # Then follow the flow only as far as a basket. Each of these is
        # optional — Ticketmaster's resale path has varied — and none of them
        # is a payment control. The allowlist is what guarantees that: a
        # button whose name is not in it is never pressed, so a future page
        # that puts "Place Order" where "Continue" used to be cannot be
        # clicked by accident.
        reached_detail = False
        for _ in range(4):
            if out_of_time():
                return result
            if _basket_is_live(page, BASKET_MARKERS):
                break
            # Did we get as far as the listing's own page? Worth recording
            # even when the answer that follows is bad news, because "never
            # reached the listing" and "reached it and it was gone" need
            # different fixes and used to read identically in the email.
            if not reached_detail and _page_says(page, LISTING_DETAIL_MARKERS, all_of=True):
                reached_detail = True
                result.note("reached the listing's own page — the click-through works")
            # A definite no. Stop at once rather than spending what is left of
            # the window pressing buttons at a dead end; the only control on
            # this screen is "Find More Tickets", which would restart the
            # search and lose the page we are on.
            if _page_says(page, LISTING_GONE_MARKERS):
                # Ask the feed before believing the page. See the forensics
                # fields on HoldResult: "sold" and "held by somebody else"
                # produce this identical screen and call for opposite
                # responses, and the difference is one call away.
                _probe_after_gone(session, event, listing, result, page)
                if result.still_listed_after:
                    result.reason = (
                        "Ticketmaster showed the 'sold or removed' page, but "
                        "the resale feed STILL lists this ticket a second "
                        "later. That is not a race we lost by being slow — it "
                        "is a listing that cannot be taken right now, most "
                        "likely sitting in somebody else's basket. Those "
                        "expire, so it is worth trying again in a few minutes."
                    )
                else:
                    result.reason = (
                        "the listing was gone by the time we clicked into it — "
                        "Ticketmaster says it has been sold or withdrawn, and "
                        "the resale feed agrees it is no longer there. This is "
                        "the race being lost at the last step, not a fault in "
                        "the watcher."
                    )
                result.note(result.reason)
                return result
            if not _press_one_safe_button(page, result):
                break
            time.sleep(1.5)

        if _basket_is_live(page, BASKET_MARKERS):
            result.secured = True
            # Prefer the clock on the page over the configured guess. The
            # guess comes from one observation of a different event, and a
            # number telling David how long he has should be measured when it
            # can be.
            seen = read_countdown_minutes(page)
            if seen is not None:
                result.minutes_hint = int(seen)
                result.minutes_measured = True
                result.note(f"countdown read from the page: {seen:.1f} min")
            else:
                result.minutes_hint = config.HOLD_MINUTES_HINT
                result.note("no countdown visible — using the configured estimate")
            result.note("BASKET CONFIRMED — the ticket is held; stopping here")

            # Where the checkout actually is, captured at the moment it exists.
            #
            # This alert deliberately carried no link, on the reasoning that a
            # basket lives in the session that created it and a link opened on
            # a phone would be an empty checkout while the real hold expired.
            # That reasoning is certainly right for a signed-OUT session and
            # may be wrong for a signed-in one: a cart bound to the ACCOUNT
            # server-side would follow David to any device he is signed in on.
            # Nobody has tested which it is here.
            #
            # So the URL is captured and offered, described as worth trying
            # rather than as the answer. Offering it costs nothing if the cart
            # does not travel — he sees an empty basket and walks to the
            # laptop, which is exactly what he would have done without it.
            # Withholding it costs the ticket on every occasion he is out and
            # it would have worked.
            result.mark("basket")
            try:
                result.checkout_url = page.url
                result.note(f"checkout URL captured: {result.checkout_url}")
            except Exception:
                pass

            # Bring it to the front so the machine he walks to is already
            # showing the thing he has to finish.
            try:
                page.bring_to_front()
            except Exception:
                pass
            return result

        if _page_says(page, LISTING_GONE_MARKERS):
            result.reason = (
                "the listing was gone by the time we clicked into it — "
                "Ticketmaster says it has been sold or withdrawn."
            )
        elif reached_detail:
            result.reason = (
                "reached the listing's own page but no basket appeared. The "
                "click-through works; it is the step after it that did not. "
                "The page text is recorded in the log."
            )
        else:
            result.reason = (
                "never reached the listing's own page — the row could not be "
                "clicked, or the page did not respond to it"
            )
        result.note(result.reason)
        return result

    except Exception as exc:
        # Never let this cost the ordinary alert. Whatever happened, David
        # still needs to be told a ticket existed.
        result.reason = f"{type(exc).__name__}: {exc}"
        result.note(f"secure attempt failed — {result.reason}")
        return result


#: Buttons this module is permitted to press, as whole-string matches.
#:
#: An allowlist rather than a denylist, because the risk is asymmetric: a
#: missing button costs a hold that David could still have got manually, and
#: an unexpected button could be the one that spends his money. Nothing that
#: completes a purchase belongs here, and nothing should be added to it
#: without a live page to check the wording against.
SAFE_BUTTONS = (
    "continue",
    "next",
    "accept and continue",
    "get tickets",
    "buy now",
    "select",
)

#: Never pressed, whatever else matches. Belt and braces around SAFE_BUTTONS:
#: if a page ever labels its payment control "Continue to payment", the
#: allowlist alone would let it through on a prefix match, so the check below
#: rejects anything containing these first.
#:
#: "find more tickets" is here for a different reason, and it is observed
#: rather than imagined: it is the button Ticketmaster puts on the dead-end
#: screen you reach when a listing has gone (see LISTING_GONE_MARKERS).
#: Pressing it throws away the listing detail page and starts the search
#: again, which would spend the rest of the 45-second window going round a
#: loop instead of reporting the truth.
#: "cancel order" is on this list for the opposite reason to the rest. It is
#: not dangerous because it spends money — it is dangerous because it throws
#: the hold away. It sits directly beside "Place Order" on the real checkout
#: page captured on 2026-08-19, which is exactly the position an automated
#: click is most likely to land on by accident.
FORBIDDEN_BUTTONS = ("pay", "place order", "confirm order", "checkout", "purchase",
                     "find more tickets", "cancel order")

#: The listing detail screen — reached by clicking a resale row.
#:
#: Observed on 2026-08-19 on a different event ("Amble", Live at the
#: Docklands), which is the same interface. This is the first direct evidence
#: that clicking a listing row leads anywhere at all, and it is why the
#: failure email can now distinguish "we never reached the listing" from "we
#: reached it and it was gone" — two failures with completely different fixes,
#: which used to produce the same message.
LISTING_DETAIL_MARKERS = ("ticket type", "section")

#: The dead end. Also observed on 2026-08-19, verbatim:
#:
#:     Sorry, these tickets are unavailable
#:     The tickets you wanted have either been sold or removed from sale.
#:
#: This is precisely the experience David described on the Electric Picnic
#: listings — the row is still on the page, and clicking it lands here. It is
#: a definite answer, not a timeout, so seeing it should stop the attempt at
#: once rather than spending the remaining seconds pressing hopefully.
LISTING_GONE_MARKERS = (
    "these tickets are unavailable",
    "sold or removed from sale",
    "tickets you wanted have either been sold",
)


def _probe_after_gone(session, event, listing, result: "HoldResult", page) -> None:
    """Ask the resale feed whether the ticket Ticketmaster just refused is gone.

    Runs the instant the dead-end screen is recognised, and never raises: this
    is diagnosis, and a failed diagnosis must not change a failure into an
    exception on the one path where David is already not getting a ticket.

    Costs one same-origin XHR from a page that is already open, which is the
    same call the sweep makes every ninety seconds. Worth it: this is the only
    moment the question can be asked, and the answer decides what the whole
    project should do next. If the ticket is still in the feed after being
    refused, then chasing seconds is chasing nothing — the listing was never
    takeable in that moment, and the winning move is to come back when the
    other basket lapses.

    Also captures the URL of the dead end, which is the only place a direct
    link to a single listing has ever been observed. If that URL turns out to
    carry the listing id, a later attempt can navigate straight to it and skip
    the navigate-quantity-search-panel sequence that costs most of the
    attempt.
    """
    try:
        result.landed_url = page.url or ""
        if result.landed_url:
            result.note(f"dead end at: {result.landed_url}")
    except Exception:
        pass

    try:
        record = session.listings_now(event, config.WANTED_QUANTITY)
        data = (record or {}).get("data")
        if not isinstance(data, dict):
            result.note("could not ask the resale feed whether it really sold")
            return
        # Both fields are checked for type before being believed. A `picks`
        # that is not a list still has a len() — a string of ten characters
        # reports ten listings — and that would answer "still listed" from
        # nothing at all, which is the one wrong answer with a cost attached:
        # it tells David to keep going back to a ticket that really has sold.
        picks = data.get("picks")
        picks = picks if isinstance(picks, list) else None
        total = data.get("total")
        total = total if isinstance(total, int) else None
        if picks is None and total is None:
            result.note(f"the resale feed answered in a shape this does not "
                        f"know how to read: keys={sorted(data)}")
            return
        result.ids_after = [str(p.get("resaleListingId") or p.get("id"))
                            for p in (picks or []) if isinstance(p, dict)
                            and (p.get("resaleListingId") or p.get("id"))]
        result.still_listed_after = bool(
            total if total is not None else len(picks))
        wanted = getattr(listing, "listing_id", "") or ""
        result.listing_id = wanted
        if result.still_listed_after:
            same = wanted and wanted in result.ids_after
            result.note(
                f"the feed still lists {len(result.ids_after) or total} ticket(s) "
                f"right after the refusal — id(s) {', '.join(result.ids_after) or '?'}"
                + (" (the same one we tried)" if same else
                   f" (we tried {wanted or '?'}, which is NOT among them)"
                   if wanted else "")
            )
        else:
            result.note("the feed agrees: nothing left. It really did go.")
    except Exception as exc:
        result.note(f"could not ask the resale feed: {type(exc).__name__}")


#: Where a button's label can hide. Read in this order, most human-meaningful
#: first, and every one of them is vetted before anything is pressed.
_LABEL_ATTRIBUTES = ("aria-label", "title", "value")


def button_labels(button) -> list:
    """Every string that might be this button's label, lowercased, no blanks.

    This exists because Playwright matches `get_by_role(name=...)` against the
    ACCESSIBLE name, and the accessible name is not always the rendered text.
    A control labelled only by `aria-label` — or by `title`, or by the `value`
    of an `<input type="submit">` — has an accessible name and no inner text
    at all.

    The guard below used to vet `inner_text()` alone. Such a button therefore
    reached is_forbidden() as the empty string, which is forbidden by nothing,
    and would have been clicked. That is not a hypothetical: it is precisely
    the hole FORBIDDEN_BUTTONS was written to close. A control whose
    accessible name is "Continue to payment" matches the allowlist entry
    "continue", renders no text of its own, and sailed through the one check
    standing between this module and David's card.

    So collect every candidate and check them all. Each source is read
    separately, because one of them raising must not cost us the others — an
    aria-label we can read is worth more than an inner_text we cannot.
    """
    labels = []

    def add(value) -> None:
        text = (value or "").strip().lower()
        if text and text not in labels:
            labels.append(text)

    try:
        add(button.inner_text(timeout=1_500))
    except Exception:
        pass
    for attribute in _LABEL_ATTRIBUTES:
        try:
            add(button.get_attribute(attribute, timeout=1_500))
        except Exception:
            pass
    return labels


def _press_one_safe_button(page, result: HoldResult) -> bool:
    """Press the first permitted button visible. True if one was pressed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for name in SAFE_BUTTONS:
        try:
            button = page.get_by_role("button", name=name, exact=False).first
            if not button.is_visible(timeout=1_500):
                continue
            labels = button_labels(button)
            # Nothing readable at all, from any source. Refuse.
            #
            # An unidentifiable button is not a safe one, and the two mistakes
            # do not cost the same: skipping a real "Continue" loses a hold
            # David could still have made by hand, while pressing a control
            # nobody could read is how this module spends his money. The
            # allowlist matched the accessible name, so SOMETHING labels this
            # button — if we cannot see what, that is a reason to stop.
            if not labels:
                result.note(
                    f"refusing to press an unlabelled button matching {name!r} "
                    f"— nothing readable to check it against"
                )
                continue
            forbidden = next((l for l in labels if is_forbidden(l)), None)
            if forbidden is not None:
                result.note(
                    f"refusing to press {forbidden!r} — that is a payment control"
                )
                continue
            button.click(timeout=5_000)
            result.note(f"pressed {labels[0]!r}")
            return True
        except (PlaywrightTimeout, PlaywrightError):
            continue
    return False


def is_forbidden(label: str) -> bool:
    """Would pressing this button risk completing a purchase?

    Substring, deliberately. "Continue to payment" must be caught by "pay",
    and it would not be by a whole-word rule.
    """
    lowered = (label or "").strip().lower()
    return any(bad in lowered for bad in FORBIDDEN_BUTTONS)


#: The countdown Ticketmaster puts on a live checkout, e.g. "11:39".
#:
#: Read rather than assumed. The hold length is the one number David has to
#: act on and it is not published; the 11:39 observed on 2026-08-19 came from
#: a different event, and there is no reason a festival resale listing must
#: get the same window as a boxing match at Croke Park. So the alert says the
#: real number when it can see one, and falls back to the configured estimate
#: only when it cannot.
COUNTDOWN_RE = __import__("re").compile(r"\b([0-9]{1,2}):([0-5][0-9])\b")



#: Longest hold worth believing. The one observed was 11:39; a match above
#: this is far more likely to be an event time than a countdown.
COUNTDOWN_MAX_MINUTES = 20


def read_countdown_minutes(page) -> Optional[float]:
    """Minutes left on the checkout clock, read off the page. None if absent.

    Three rules, each earning its place, and all three are needed because a
    checkout page is full of times that are not the hold — the event's own
    start time most of all.

    1. WHOLE LINE ONLY. A countdown stands alone on its line; every other time
       on a checkout is embedded in a sentence — "Sat, 5 Sept 2026, 16:00",
       "Doors 19:00". Matching anywhere in the text reports the event's start
       time as the time remaining, because 16:00 parses as a perfectly
       plausible sixteen-minute hold. On the page captured on 2026-08-19 the
       countdown was alone on its own line, printed twice, above the word
       "Checkout".

    2. NOT ON THE MINUTE. A time written mm:00 is almost always a clock rather
       than a countdown: an event time is written 16:00, while a countdown
       shows :00 for one second in sixty. Skipping those costs a measurement
       once in every sixty holds and avoids reporting an event time as the
       time remaining.

    3. FIRST DOWN THE PAGE, not smallest. The captured page put the countdown
       at the very top, and it is the page's own most prominent clock. This
       used to take the smallest match anywhere instead, which worked on the
       real page purely by luck — 11:39 happened to be smaller than everything
       else on it — and would have preferred a stray "02:15" further down over
       the genuine countdown above it.

    Every rule fails towards None, and None means the alert uses
    config.HOLD_MINUTES_HINT and says it is an estimate. Getting this wrong in
    the cautious direction costs the measurement; getting it wrong the other
    way tells David he has sixteen minutes when he has two.

    One hole is left open knowingly: a bare "19:30" alone on a line would
    still read as a 19½-minute hold. Nothing observed does that, and the only
    fix would be to require the countdown to sit near a word like "left",
    which no captured page reliably carries.
    """
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return None

    for line in text.splitlines():
        match = COUNTDOWN_RE.fullmatch(line.strip())
        if not match:
            continue
        mins, secs = int(match.group(1)), int(match.group(2))
        if secs == 0:
            continue                      # a clock, not a countdown — see rule 2
        minutes = mins + secs / 60.0
        if 0 < minutes <= COUNTDOWN_MAX_MINUTES:
            return minutes                # first one down the page — see rule 3
    return None


def describe_lapse(days_left) -> str:
    """"in about 3 hours", "in 12 days", "already" — never "in 0 day(s)".

    The old wording printed `days_left` rounded to one decimal and appended
    "day(s)", so anything under about ninety minutes rendered as "0 day(s)" —
    which is either alarming or meaningless depending on how closely it is
    read, and is exactly the line that decides whether David goes and signs in
    again before a listing appears. Seen for real in `doctor` on 2026-08-19.

    Deliberately vague above a day and precise below one, because that is
    where the accuracy is worth anything: "12 days" needs no action today,
    "about 2 hours" does.
    """
    if days_left is None:
        return "at an unknown time"
    if days_left <= 0:
        return "already"
    hours = days_left * 24.0
    if hours * 60 < 1:
        # Positive but under a minute. "in about 0 minutes" is the shape of
        # bug this function exists to prevent, so say the true thing instead.
        return "within the minute"
    if hours < 1.5:
        return f"in about {max(1, int(round(hours * 60))) } minutes"
    if days_left < 1:
        return f"in about {hours:.0f} hours"
    if days_left < 2:
        return "in about a day"
    return f"in about {days_left:.0f} days"


def _page_says(page, markers, all_of: bool = False) -> bool:
    """Is this page showing these words? Never raises.

    `all_of` distinguishes the two kinds of question asked of it. A dead end
    is recognised by any one of several phrasings, while the listing detail
    screen is recognised by several labels appearing TOGETHER — "section"
    alone appears on the search results too, so any-of would call every page
    the detail page.
    """
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    hits = (marker in text for marker in markers)
    return all(hits) if all_of else any(hits)


def _basket_is_live(page, markers) -> bool:
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in markers)


def _find_listing_row(page, listing, result: HoldResult):
    """The clickable row for this listing, or None.

    Prefers the section, which is the one field both the API and the rendered
    page agree on. Falls back to any resale row, because one listing is the
    overwhelmingly common case — of the nine sightings up to 2026-08-18,
    every one was a single listing.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    section = getattr(listing, "section", None)
    if section:
        try:
            row = page.get_by_text(f"Section {section}", exact=False).first
            if row.is_visible(timeout=5_000):
                result.note(f"found the row for Section {section}")
                return row
        except (PlaywrightTimeout, PlaywrightError):
            result.note(f"no row matched Section {section} — trying any resale row")

    try:
        row = page.get_by_text("Verified Resale Ticket", exact=True).first
        if row.is_visible(timeout=5_000):
            result.note("found a Verified Resale row")
            return row
    except (PlaywrightTimeout, PlaywrightError):
        pass
    return None


class _NoteSink:
    """Adapter so browser.py's Reading-shaped helpers can write into a HoldResult.

    `_set_quantity` takes something with .note(); a HoldResult has one, but
    going through an adapter keeps the two types from growing into each other.
    """

    def __init__(self, result: HoldResult):
        self._result = result

    def note(self, text: str) -> None:
        self._result.note(text)
