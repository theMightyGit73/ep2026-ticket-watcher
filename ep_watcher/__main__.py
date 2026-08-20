"""Command line entry point:  python -m ep_watcher <command>"""

import argparse
import datetime
import json
import os
import random
import sys
import time
from pathlib import Path

from . import config, engine, events, notify, state as state_mod
from .sources import discovery, inventory_api
from .state import stamp


def _browser():
    """Import the browser source only when a command actually needs it.

    Playwright is absent in API-only deployments (EP_USE_BROWSER=0), so a
    module-scope import would stop `run`, `status` and `selftest` working
    anywhere the browser cannot run — which is exactly where those commands
    are most useful.
    """
    from .sources import browser

    return browser


def _banner(title: str) -> None:
    print(f"\n[{stamp()}] {title}")
    print(f"  {config.EVENT_NAME}")
    print(f"  {config.EVENT_URL}\n")


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_login_auto(_args) -> int:
    """Sign the buying profile in from stored credentials, no keyboard needed.

    On the `automated-login` branch only, and opt-in there. `login-buy` is
    still the supported path and needs no password to exist anywhere.

    Verification is deliberately NOT the page's own say-so. Ticketmaster
    renders no account text Playwright can read — established on 2026-08-19
    against nine real captures — so success is judged the same way check-buy
    judges it: did account cookies actually appear in the profile.
    """
    from . import autologin, buyer

    _banner("Signing the BUYING profile in automatically")
    print(f"  Profile: {config.BUY_PROFILE_DIR}")

    if not config.have_login_credentials():
        print("\n  [FAIL]  TM_EMAIL and TM_PASSWORD are not set.\n")
        print("  Add them to ~/.ep2026-watcher/env — the file run_watcher.sh")
        print("  sources, chmod 600. Never on the command line: an argument is")
        print("  visible in `ps` to every process on this machine.\n")
        print("      printf 'TM_EMAIL=you@example.com\\n' >> ~/.ep2026-watcher/env")
        print("      read -rs P && printf 'TM_PASSWORD=%s\\n' \"$P\" >> ~/.ep2026-watcher/env")
        print("      chmod 600 ~/.ep2026-watcher/env\n")
        print("  `read -rs` keeps it off the screen and out of shell history.\n")
        return 1

    before = set(buyer.profile_cookies(config.BUY_PROFILE_DIR))
    config.OFFSCREEN = False          # visible, so a challenge can be finished by hand
    session = _browser().BrowserSession(headless=False, profile_dir=config.BUY_PROFILE_DIR)
    try:
        session.start()
        result = autologin.sign_in(session)
        # Cookies are written as the page settles; give Chrome a moment before
        # reading them, the same reason login-buy waits before fingerprinting.
        time.sleep(3)
        gained = set(buyer.profile_cookies(config.BUY_PROFILE_DIR)) - before
    finally:
        session.close()

    print()
    if result.outcome == "challenged":
        print(f"  [ -- ]  {result.reason}\n")
        return 2
    if result.outcome in ("rejected", "no-form", "error"):
        print(f"  [FAIL]  {result.reason}\n")
        return 1

    record = buyer.record_signed_in_fingerprint(config.BUY_PROFILE_DIR)
    auth = record.get("auth_cookies") or []
    if not auth:
        print("  [FAIL]  the form went through but no account cookies appeared.")
        print("          That usually means the sign-in did not actually take.")
        print("          Try `login-buy` and do it by hand.\n")
        return 1

    print(f"  [ OK ]  signed in — {len(auth)} account cookie(s) recorded")
    for name in auth[:6]:
        print(f"            · {name}")
    if gained:
        print(f"          ({len(gained)} cookie(s) are new since before the attempt)")
    print("\n  Confirm any time with:  python -m ep_watcher check-buy\n")
    return 0


def cmd_check_buy(_args) -> int:
    """Is the buying profile signed in and ready to hold a ticket?

    The question this answers is the one that cannot be answered by looking
    at the config: securing is armed and the flag is set, but is the SESSION
    still good? Cookies expire, Ticketmaster invalidates them, and a profile
    reset wipes them. All three fail silently and identically — the first
    symptom is a listing appearing and not being held, which is the one
    moment there is no time to investigate.

    Deliberately read-only. It opens the page, reads whether the account is
    present, and closes. It never types a credential, never clicks a listing,
    and never puts anything in a basket. Signing in remains a thing David
    does by hand in `login-buy`, so no password is ever stored or replayed —
    which is also why there is nothing here that could lock the account.
    """
    from . import buyer

    _banner("Checking the buying profile")
    if not config.BUY_PROFILE_DIR.exists():
        print(f"  [FAIL]  no buying profile at {config.BUY_PROFILE_DIR}")
        print("\n  Fix:  python -m ep_watcher login-buy\n")
        return 1

    # Answered from the cookie database, not by opening a browser. Reading
    # the page cannot answer it: Ticketmaster renders no account text that
    # Playwright can see, which was checked against every capture the watcher
    # has taken. Cookies are also cheaper, need no network, and cannot get
    # this profile challenged for asking.
    ev = buyer.session_evidence(config.BUY_PROFILE_DIR)

    if ev["signed_in"] is True:
        print(f"  [ OK ]  signed in — {ev['reason']}")
        if ev["days_left"] is not None:
            # Information, not a verdict. Nothing here can know when
            # Ticketmaster will end the session — it can do so server-side
            # whenever it likes — and two attempts at a confident number both
            # misled on 2026-08-19. What IS reliable is whether the recorded
            # cookies are still in the profile, which is the line above.
            print(f"  [ -- ]  first account cookie lapses "
                  f"{buyer.describe_lapse(ev['days_left'])} "
                  f"({ev['expires_at'][:16]})")
            print("          That may or may not end the session. Re-run this")
            print("          check afterwards rather than assuming either way.")
        print(f"\n  Profile: {config.BUY_PROFILE_DIR}\n")
        return 0

    if ev["signed_in"] is False:
        print(f"  [FAIL]  not signed in — {ev['reason']}")
        print("\n  Fix:  python -m ep_watcher login-buy\n")
        return 1

    print(f"  [ -- ]  cannot tell — {ev['reason']}")
    print("\n  Re-run login-buy to record what a signed-in session looks like.\n")
    return 1


def cmd_login(_args) -> int:
    """Sign in by hand, once. The cookies land in the profile dir and every
    later run reuses them — this is the whole reason the watcher can see a
    page that returns 401 to everything else."""
    _banner("Opening Chrome so you can sign in to Ticketmaster")
    print("  1. Accept the cookie dialog if it appears.")
    print("  2. Sign in (only needed for buying — watching works logged out).")
    print("  3. Come back here and press Enter.\n")

    config.OFFSCREEN = False  # he needs to actually see and use this window
    with _browser().BrowserSession(headless=False) as session:
        try:
            session.page.goto(config.EVENT_URL, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"  (navigation hiccup, carry on in the window anyway: {exc})")
        try:
            input("  Press Enter when you're signed in and the page looks right... ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return 1
        text = session.visible_text().lower()

    if "sign out" in text or "my account" in text:
        print(f"\n  Signed in. Session saved to {config.PROFILE_DIR}")
        return 0
    print("\n  Could not confirm a signed-in session — run `check` and see what it says.")
    return 0


def cmd_login_buy(_args) -> int:
    """Sign in the BUYING profile — the only one that ever carries the account.

    Separate from `login` and separate from the watcher's profile on purpose.
    The watcher polls signed out, roughly 140 times a day, so a block costs a
    profile reset and nothing else. This profile is opened only when a real
    listing exists and is the one that holds a basket.

    Nobody's password is stored, typed or read by this program. It opens a
    window; David signs in the way he would on any other day; Ticketmaster
    leaves cookies in the profile directory and those are what later runs use.
    That is why the watcher never needs to be told a password — and it should
    never be changed to accept one.
    """
    _banner("Opening Chrome so you can sign in the BUYING profile")
    print(f"  Profile: {config.BUY_PROFILE_DIR}")
    print("  This is the session that will hold a basket for you.\n")
    print("  1. Accept the cookie dialog if it appears.")
    print("  2. Sign in to the Ticketmaster account you want to BUY with.")
    print("  3. Come back here and press Enter.\n")

    config.OFFSCREEN = False
    with _browser().BrowserSession(
        headless=False, profile_dir=config.BUY_PROFILE_DIR
    ) as session:
        # Land on the sign-in page, not the event page.
        #
        # This used to open the event URL, on the assumption that signing in
        # from there was obvious. It is not: David ran it on 2026-08-19, was
        # taken straight to the Electric Picnic listing, and was never
        # prompted for anything. Ticketmaster does not ask — the account
        # control is an icon in the top bar, and it is the same control this
        # project already established is invisible to Playwright's flattened
        # text. Being dropped on a page with no visible next step is how a
        # one-command setup becomes a support conversation.
        #
        # The candidates are tried in order because the exact path is not
        # something to be confident about from memory; the event page remains
        # the last resort, so the window always opens on something usable and
        # he can navigate by hand if none of them land.
        landed = ""
        for candidate in config.SIGNIN_URLS + (config.EVENT_URL,):
            try:
                response = session.page.goto(candidate, wait_until="domcontentloaded")
            except Exception:
                continue
            if response is None or response.status < 400:
                landed = candidate
                break
        if landed:
            print(f"  Opened: {landed}\n")
        else:
            print("  (could not open any page — sign in by hand in the window)\n")
        print("  If you see the event page rather than a sign-in form, click the")
        print("  account icon in the top bar, or type ticketmaster.ie/member in")
        print("  the address bar. Ticketmaster never prompts on its own.\n")
        try:
            input("  Press Enter when you're signed in and the page looks right... ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return 1
    # Take the fingerprint AFTER the browser has closed, so Chrome has
    # flushed its cookie database to disk. Reading it while the window is
    # still open can miss the very cookies the sign-in just created.
    from . import buyer

    record = buyer.record_signed_in_fingerprint(config.BUY_PROFILE_DIR)
    auth = record.get("auth_cookies") or []
    if auth:
        # This is the only moment anyone can know for certain what a
        # signed-in profile looks like, because a human has just said so.
        # Every later check compares against what is recorded here rather
        # than against a guess — see buyer.session_evidence().
        print(f"\n  Signed in. Buying session saved to {config.BUY_PROFILE_DIR}")
        print(f"  Recorded {len(auth)} account cookie(s) so the session can be")
        print(f"  checked later without opening a browser:")
        for name in auth[:6]:
            print(f"    · {name}")
        if len(auth) > 6:
            print(f"    · ...and {len(auth) - 6} more")
        print("\n  Check it any time with:  python -m ep_watcher check-buy\n")
        return 0

    print("\n  Could not confirm a signed-in session — the profile holds only")
    print("  the cookies an anonymous visitor gets. If you did sign in, the")
    print("  window may have been closed before Chrome saved them; try again")
    print("  and give it a moment before pressing Enter.\n")
    return 1


def cmd_check(_args) -> int:
    """Read every watched event once and print the results. Sends nothing."""
    print(f"\n[{stamp()}] Manual check of {len(config.EVENTS)} event(s) — no notifications\n")

    worst = 0
    session = None
    try:
        if config.USE_BROWSER:
            # One browser for all events rather than a cold start each: the
            # page load is the cheap part, launching Chrome is not.
            #
            # Its own profile, because Chrome locks a user-data-dir and the
            # service is usually running — checking by hand must not require
            # stopping the thing being checked.
            session = _browser().BrowserSession(
                profile_dir=config.PROFILE_DIR.parent / "chrome-profile-check"
            )
            session.start()
        for event in config.EVENTS:
            worst = max(worst, _print_reading(engine.poll(session, event)))
    finally:
        if session:
            session.close()
    return worst


def _print_reading(reading) -> int:
    print(f"  {reading.event_name}")
    print(f"  {reading.event_url}")

    print("─" * 68)
    print(f"  Primary (box office) : {reading.primary}")
    print(f"  Resale (verified)    : {reading.resale}")
    print(f"  Source               : {reading.source}")
    if reading.failed:
        print("  Result               : FAILED — no usable reading")
    elif reading.any_good:
        print("  Result               : SOMETHING IS AVAILABLE")
    else:
        print("  Result               : nothing available")
    for note in reading.notes:
        print(f"  · {note}")
    for listing in reading.listings:
        print(f"  → {listing.kind}: {listing.describe()}")
    print("─" * 68)
    return 1 if reading.failed else 0


def cmd_run(_args) -> int:
    """One full cycle including alerts. This is what a scheduler calls."""
    if _stop_if_past_date():
        return 0
    reading = engine.run_once()
    return 1 if reading.failed else 0


def securing_banner() -> list:
    """The "Securing: ..." lines printed on every start of `watch`.

    Securing is the one setting that changes what the watcher DOES rather than
    how often it looks, so it is stated on every start. And it is stated
    loudly when it is armed but cannot work: an unsigned buying profile fails
    only at the moment a real listing appears, which is the worst possible
    time to discover it — the flag was enabled on 2026-08-19 with the profile
    not yet created, and nothing anywhere said so.

    Asked of the cookies, not of the filesystem. This used to test whether the
    buying profile's Cookies database existed, which is a question with a
    reassuring answer and almost no meaning: a signed-OUT ticketmaster.ie
    profile carries 33 cookies, so that file appears the moment the buying
    browser has loaded a single page. The case this banner exists to catch — a
    profile that WAS signed in and has since been signed out, or whose account
    cookies have lapsed — passed it in silence, and would have announced
    itself for the first time at the one moment there is no time left to
    investigate. session_evidence() is what `doctor` and `check-buy` already
    ask, so all three now give the same answer.

    Returned as lines rather than printed, so the decision can be tested
    against a profile fixture without starting a watcher.
    """
    if not config.SECURE_ON_FIND:
        return ["  Securing: off — notify only (EP_SECURE_ON_FIND=1 to enable)"]

    from . import buyer

    lines = ["  Securing: ON — will hold a resale listing, never pay for it"]
    evidence = buyer.session_evidence(config.BUY_PROFILE_DIR)
    if evidence["signed_in"] is True:
        lines.append(f"    signed in — {evidence['reason']}")
    elif evidence["signed_in"] is False:
        lines.append(f"    ⚠ the buying profile is NOT signed in ({evidence['reason']}),")
        lines.append("      so securing cannot work yet. Run:")
        lines.append("          python -m ep_watcher login-buy")
    else:
        lines.append("    ⚠ cannot tell whether the buying profile is signed in")
        lines.append(f"      ({evidence['reason']}). Confirm with:")
        lines.append("          python -m ep_watcher check-buy")
    return lines


def cmd_watch(args) -> int:
    """Long-running loop holding one warm browser open between polls.

    Preferred over scheduling one-shot `run`s: the session stays warm, each
    poll costs a page load instead of a browser cold start, and a persistent
    real browser is a far more ordinary thing to be doing than a fresh
    headless Chrome every two minutes.
    """
    interval = args.interval or config.POLL_INTERVAL_SECONDS
    # The press-mode floor bounds how often a PAGE is searched, not how often
    # the loop wakes. See config.PRESS_MIN_INTERVAL_SECONDS: raising the tick
    # here used to be the same thing and has not been since pages got their
    # own intervals — it only blunted the clock, while the requests carried on
    # at whatever the per-page ranges said.
    if config.PRESS_THE_BUTTON:
        rushed = [e for e in config.EVENTS
                  if e.searchable()
                  and e.fastest_gap_seconds < config.PRESS_MIN_INTERVAL_SECONDS]
        for event in rushed:
            print(
                f"[{stamp()}] press mode: {event.slug} can draw a gap of "
                f"{event.fastest_gap_seconds}s, under the "
                f"{config.PRESS_MIN_INTERVAL_SECONDS}s floor — each search is a "
                f"real reserve attempt, and sustained polling this fast is what "
                f"produced the 403s. Raise its EP_*_PEAK_MIN."
            )

    # Refuse to run blind. A watcher that polls perfectly for two weeks and
    # cannot send mail is worse than no watcher: it looks like it is working,
    # and the silence reads as "no tickets yet" rather than "no email set up".
    # That is the exact shape of the failure this whole rewrite exists to fix.
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        print("\n  REFUSING TO START: no email configured.\n")
        print("  Nothing would ever reach you, and the silence would look")
        print("  identical to 'no tickets yet'.\n")
        print(f"  Put a Gmail app password in {config.REPO_DIR.home()}/.ep2026-watcher/env")
        print("  (https://myaccount.google.com/apppasswords — needs 2FA), then:")
        print("      ./run_watcher.sh test\n")
        print("  Override with EP_ALLOW_NO_EMAIL=1 if you really mean it.\n")
        if os.environ.get("EP_ALLOW_NO_EMAIL", "").lower() not in ("1", "true", "yes"):
            return 1

    # Before anything expensive. On the morning after the event this would
    # otherwise launch a whole browser purely to throw it away one line later.
    if _stop_if_past_date():
        return 0

    mode = "PRESS THE BUTTON" if config.PRESS_THE_BUTTON else "read-only"
    _banner(f"Watching every ~{interval}s · mode: {mode}")
    if config.NIGHT_POLL_SECONDS:
        print(
            f"  Overnight ({config.NIGHT_START_HOUR:02d}:00-{config.NIGHT_END_HOUR:02d}:00 "
            f"local): every ~{config.NIGHT_POLL_SECONDS // 60} min"
        )
    if discovery.configured():
        print("  Discovery API: configured")
    if inventory_api.configured():
        print("  Inventory Status API: configured")

    # Securing is the one setting that changes what the watcher DOES rather
    # than how often it looks, so it says so on every start. And it says so
    # loudly when it is armed but cannot work: an unsigned buying profile
    # fails only at the moment a real listing appears, which is the worst
    # possible time to discover it — the flag was enabled on 2026-08-19 with
    # the profile not yet created, and nothing anywhere said so.
    for line in securing_banner():
        print(line)

    # Which pages this run will NOT look at. Printed next to the securing
    # banner because it answers the same class of question — what does this
    # watcher actually do — and because a page silently missing from the log
    # is indistinguishable from a page that is failing.
    for event in config.paused_pages():
        print(f"  {event.name}: NOT SEARCHED (EP_EARLY_ENTRY=1 to turn on)")

    if not config.USE_BROWSER:
        # API-only: no browser to keep warm, so this is just a polling loop.
        print("  Browser DISABLED — API sources only\n")
        return _watch_apis_only(interval)

    # A second watcher elsewhere starts half a tick out of step, so the two
    # look at the page at different moments rather than together. Done before
    # the browser opens: waiting with Chrome already running would hold the
    # profile lock for nothing.
    if config.POLL_PHASE:
        offset = interval * config.POLL_PHASE
        print(
            f"[{stamp()}] phase offset {config.POLL_PHASE:.2f} — waiting "
            f"{offset / 60:.1f} min so this watcher interleaves with the other"
        )
        time.sleep(offset)

    events.emit("start", interval=interval,
                securing=config.SECURE_ON_FIND, sweep=config.RESALE_SWEEP,
                night=config.is_night())
    session = _start_session()
    # Start the identity clock if nothing has ever set it, so the pre-emptive
    # refresh has an age to measure against. Stamping now rather than assuming
    # the worst avoids throwing away a profile that may be minutes old every
    # time the service restarts. Done here rather than inside _start_session:
    # opening a browser should not write state, and when it did, a unit test
    # exercising the retry path wrote to the live state file.
    _st = state_mod.load()
    if _st.get("profile_reset_at") is None:
        state_mod.note_profile_reset(_st)
        state_mod.save(_st)
    backoff = 0
    tried_profile_reset = False
    was_night = config.is_night()
    # Lives across the whole run, so its per-event clocks and its refusal
    # count survive between cycles. See engine.ResaleSweep.
    # The warm buying browser, started alongside the watcher's own. Opening
    # it now is the whole point: a cold Chrome launch plus the event page's
    # 401-reload dance was most of the sixty seconds between seeing a listing
    # and clicking it, and these listings do not last sixty seconds.
    #
    # Nothing waits on it and nothing depends on it. If it never comes up,
    # secure_in_thread falls back to the cold start it has always used.
    buy_worker = None
    if config.SECURE_ON_FIND and config.WARM_BUY_BROWSER:
        from . import buyer as _buyer

        buy_worker = _buyer.BuyerWorker()
        buy_worker.start()
        engine.set_buy_worker(buy_worker)
        print("  Buying browser: warming in the background "
              "(cold start used if it fails)")

    sweep = engine.ResaleSweep()
    if config.RESALE_SWEEP:
        print(f"  Resale sweep: every ~{config.RESALE_SWEEP_SECONDS}s between "
              f"searches (resale only, one XHR per page)")
    try:
        while True:
            if _stop_if_past_date():
                return 0
            try:
                reading = engine.run_once(session)

                # Being blocked and carrying on at the normal cadence is how a
                # short rate-limit becomes a long one. But before assuming the
                # network is at fault, try a clean browser identity: the block
                # is carried in the profile's bot-check cookies, and a fresh
                # profile has been observed clearing it instantly on a network
                # where the old profile was still refused. Only if that fails
                # is it really the IP, and only then is a long sleep the
                # right answer.
                if reading.blocked:
                    if not tried_profile_reset:
                        print(f"[{stamp()}] blocked — trying a clean browser profile first")
                        session.reset_profile()
                        _mark_profile_reset()
                        tried_profile_reset = True
                        time.sleep(30)
                        continue
                    backoff = min(backoff * 2 or config.BLOCKED_BACKOFF_SECONDS,
                                  config.BLOCKED_BACKOFF_MAX_SECONDS)
                    resting = backoff * random.uniform(0.85, 1.15)
                    print(
                        f"[{stamp()}] still blocked with a fresh profile — this is the "
                        f"network. Sleeping {resting // 60:.0f} min."
                    )
                    # Say so in state before sleeping. Otherwise the frozen
                    # last_check_at is indistinguishable from a hang, and past
                    # 45 minutes the watchdog restarts a watcher that is
                    # resting on purpose — which polls the rate-limited
                    # connection again immediately and deepens the block.
                    _mark_backoff(resting)
                    time.sleep(resting)
                    _mark_backoff(None)
                    continue
                if backoff or tried_profile_reset:
                    print(f"[{stamp()}] recovered from rate limiting")
                    backoff = 0
                    _mark_backoff(None)
                tried_profile_reset = False
                # A live basket has a countdown on it; stop polling and leave
                # the window alone so David can actually finish the checkout.
                if config.PRESS_THE_BUTTON and any("RESERVE ACCEPTED" in n for n in reading.notes):
                    _pause_for_checkout()
                    continue
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{stamp()}] poll raised {type(exc).__name__}: {exc} — restarting browser")
                session.close()
                time.sleep(10)
                session = _browser().BrowserSession()
                session.start()

            # Recomputed every loop, not once at startup: the watcher runs
            # for days, so it has to notice night beginning and ending while
            # already running.
            wait, night = config.poll_interval_now(interval)
            if night != was_night:
                print(
                    f"[{stamp()}] "
                    + (
                        f"overnight — slowing to {wait // 60} min between checks"
                        if night
                        else f"morning — back to {wait // 60} min between checks"
                    )
                )
                was_night = night
            # Tell the watchdog when to expect the next poll, so it judges
            # lateness against the cadence actually in force rather than
            # against a fixed number that only matched daytime.
            sleeping = wait * random.uniform(0.75, 1.25)
            _mark_next_poll(sleeping)
            # Step around the wall rather than into it. Every one of the 28
            # blocks recorded in six days was cleared by a fresh profile on
            # the first attempt, so the identity — not the address — is what
            # ages out. Doing it here spends the sleep window, not a poll.
            # A hold that has run out its clock frees the warm browser. Done
            # here rather than on a timer because this is the loop that
            # already knows the hold is over — it is the same condition the
            # watchdog uses to decide it may restart the watcher again.
            if buy_worker is not None and buy_worker.holding:
                if state_mod.hold_remaining(state_mod.load()) <= 0:
                    print(f"[{stamp()}] hold window over — releasing the buying browser")
                    buy_worker.release()
            _refresh_profile_if_stale(session)
            # Same reasoning, same window: the daily copy of the env file, the
            # state and the signed-in session costs a second or two and must
            # never come out of the time budget for a search.
            _maybe_backup()
            _sleep_and_sweep(session, sweep, sleeping)
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] Stopped.")
        return 0
    finally:
        session.close()
        if buy_worker is not None:
            engine.set_buy_worker(None)
            # Shutdown deliberately does NOT close a browser that is holding
            # something. If David is mid-checkout when this process stops, the
            # basket is his and the window stays.
            buy_worker.shutdown()


def _sleep_and_sweep(session, sweep, seconds: float) -> None:
    """Sleep, but ask the resale endpoint every so often on the way through.

    The sleep between searches is the watcher's blind window, and on
    2026-08-20 it was measured as the thing actually costing the tickets: a
    listing had been live ~3.25 minutes on average before a search found it,
    and every securing attempt that reached Ticketmaster found it gone. This
    turns a dead wait into a cheap watch.

    Chunked rather than one long sleep so the loop still wakes on time — the
    remainder is slept exactly, so the search cadence the budget was
    calculated from is unchanged. The sweep adds calls; it does not shorten
    the gap between searches.

    A find inside the sweep ends the sleep early. The alert has already gone
    out by then and a hold may be live, so there is no reason to lie in bed
    for the rest of a window whose purpose has just been served.
    """
    if not config.RESALE_SWEEP or sweep is None or sweep.stopped:
        time.sleep(seconds)
        return

    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(5.0, left))
        # Asked from memory before the state file is touched at all: this loop
        # wakes every few seconds and the sweep is due every ninety, so all but
        # one wake in eighteen has nothing to do.
        if not sweep.any_due(time.monotonic()):
            continue
        try:
            st = state_mod.load()
            if sweep.run(session, st) is not None:
                state_mod.save(st)
                return
        except Exception as exc:
            # Never let the cheap extra look cost the expensive scheduled one.
            print(f"[{stamp()}] resale sweep skipped: {type(exc).__name__}: {exc}")


def _pause_for_checkout() -> None:
    """Stop polling while a basket is live, without getting ourselves killed.

    This used to be `while True: time.sleep(30)` and nothing else, which was
    two separate mistakes stacked on each other.

    The first is that it wrote nothing down. The watchdog restarts a watcher
    whose poll clock has stopped advancing, which is correct in every case but
    this one — and a paused checkout looks exactly like a hung Chrome from
    outside. Fifteen minutes after the pause began, `launchctl kickstart -k`
    would have killed the process, and the basket lives in the browser that
    process launched, so the ticket would have gone with it. The one thing
    worse than missing a ticket is destroying one already caught, and the
    machinery doing the destroying would have been the machinery meant to keep
    the watch alive.

    The second is that it never ended. "Ctrl-C when you're done" is fine at a
    terminal and meaningless under launchd, where nobody is at a keyboard: a
    hold David never noticed would have stopped the watch until somebody
    thought to look, which is the ambiguous silence this whole project exists
    to refuse.

    So it is bounded, and it says so in state on every pass — the marker is
    refreshed rather than set once, so it stays honest if the pause is cut
    short, and it lapses on its own if it is not.
    """
    minutes = config.hold_window_minutes()
    print(f"[{stamp()}] Reserve accepted — pausing the loop so you can check out.")
    print(f"  The browser is holding the basket. Nothing will restart the")
    print(f"  watcher for {minutes:.0f} min; after that it goes back to watching.")

    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        left = (deadline - time.monotonic()) / 60.0
        # Rewritten every pass so the marker never expires mid-checkout, and
        # so it shrinks honestly rather than claiming the full window forever.
        st = state_mod.load()
        state_mod.note_hold(st, left)
        # The poll clock too: the watchdog checks whichever of the two it
        # finds, and a stale next_poll_due is the other way it decides a
        # watcher is late.
        state_mod.note_next_poll(st, left * 60)
        state_mod.save(st)
        time.sleep(min(30.0, max(1.0, (deadline - time.monotonic()))))

    st = state_mod.load()
    state_mod.clear_hold(st)
    state_mod.save(st)
    print(f"[{stamp()}] checkout window over — watching again")


def _mark_backoff(seconds) -> None:
    """Write the deliberate-idle marker, or clear it with None.

    Loaded and saved on its own rather than threaded through engine.run_once,
    because the backoff happens *between* cycles — run_once has already saved
    and closed its copy, so there is nothing to race with.
    """
    st = state_mod.load()
    if seconds is None:
        state_mod.clear_backoff(st)
    else:
        state_mod.note_backoff(st, seconds)
    state_mod.save(st)


def _mark_profile_reset() -> None:
    """Record that the browser identity was just rebuilt."""
    st = state_mod.load()
    state_mod.note_profile_reset(st)
    state_mod.save(st)


def _refresh_profile_if_stale(session) -> bool:
    """Rebuild the browser identity before Ticketmaster refuses it.

    Done between polls, in the time that would have been spent asleep, so it
    costs nothing a poll would otherwise have used. Failure here is not fatal:
    the old profile keeps working until it is refused, and the reactive reset
    still covers that.
    """
    st = state_mod.load()
    if not state_mod.profile_is_stale(st):
        return False
    age = state_mod.profile_age_minutes(st) or 0.0
    print(
        f"[{stamp()}] browser identity is {age:.0f} min old "
        f"(limit {config.PROFILE_MAX_AGE_MINUTES:.0f}) — refreshing it before "
        f"Ticketmaster does it for us"
    )
    try:
        session.reset_profile()
        _mark_profile_reset()
        return True
    except Exception as exc:
        print(f"[{stamp()}] pre-emptive profile refresh failed: {type(exc).__name__}: {exc}")
        return False


def _mark_next_poll(seconds: float) -> None:
    """Record when the next poll is due, for the watchdog to measure against."""
    st = state_mod.load()
    state_mod.note_next_poll(st, seconds)
    state_mod.save(st)


def _start_session(attempts: int = 3):
    """Open the browser, retrying a held profile lock rather than dying on it.

    Chrome takes an exclusive lock on its user-data-dir, and restart.sh kills
    the old Chrome moments before launchd starts the new watcher. If that lock
    has not been released yet, start() raises — and this happens *before* the
    poll loop, so it escaped the loop's own error handling and killed the
    process. launchd does bring it back, but the one command David is told to
    run when something is wrong must not have a chance of leaving nothing
    running at all. Observed as a double start during the 2026-08-16 restart.

    Gives up after `attempts` and re-raises, because a browser that genuinely
    cannot start is a real failure and exiting lets launchd retry with a clean
    process rather than looping here forever.
    """
    last = None
    for attempt in range(1, attempts + 1):
        session = _browser().BrowserSession()
        try:
            session.start()
            if attempt > 1:
                print(f"[{stamp()}] browser started on attempt {attempt}")
            return session
        except Exception as exc:
            last = exc
            print(
                f"[{stamp()}] browser start failed "
                f"(attempt {attempt}/{attempts}): {type(exc).__name__}: {exc}"
            )
            session.close()
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise last


def _stop_if_past_date() -> bool:
    """Shut down cleanly once the event has been and gone.

    Sends one final email so the silence that follows is explained rather than
    ambiguous, and marks it in state so a restart doesn't send it again.

    Exiting 0 matters: the LaunchAgent is configured to restart only on an
    *unsuccessful* exit, so a clean exit here is what actually makes the
    watcher stay stopped instead of being revived every few seconds.
    """
    if not state_mod.past_stop_date():
        return False

    st = state_mod.load()
    print(f"\n[{stamp()}] Past the stop date ({config.STOP_AFTER_DATE}) — shutting down.")
    if not st.get("stop_notified"):
        notify.stopped(st.get("checks_total", 0))
        st["stop_notified"] = True
        state_mod.save(st)
    print("  Nothing further will run. To watch a later event, set EP_STOP_AFTER")
    print("  to a new date and start it again.\n")
    return True


def _watch_apis_only(interval: int) -> int:
    """Polling loop with no browser to keep alive."""
    try:
        while True:
            if _stop_if_past_date():
                return 0
            try:
                engine.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{stamp()}] poll raised {type(exc).__name__}: {exc}")
            time.sleep(interval * random.uniform(0.75, 1.25))
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] Stopped.")
        return 0


def cmd_events(_args) -> int:
    """Answer the questions the prose log cannot.

    Written after an afternoon of grepping watcher.log with throwaway parsers,
    one of which reported nearly every poll as a find because
    `"AVAILABLE" in "UNAVAILABLE"` is true. The answer was wrong and looked
    entirely plausible, which is the argument for having data rather than
    prose to query.
    """
    from . import events as ev

    records = ev.read()
    if not records:
        print(f"\n  No events recorded yet — {ev.path()}\n")
        return 0

    _banner(f"{len(records)} events at {ev.path()}")
    counts = ev.summarise(records)
    print("  What is in the log:")
    for kind, n in counts.items():
        print(f"    {kind:10} {n:6}")

    finds = [r for r in records if r.get("kind") == "find"]
    holds = [r for r in records if r.get("kind") == "hold"]
    polls = [r for r in records if r.get("kind") == "poll"]

    if polls:
        blind = sum(1 for r in polls if r.get("resale") == "UNKNOWN")
        print(f"\n  Polls: {len(polls)}, resale unreadable on {blind} "
              f"({blind / len(polls) * 100:.1f}%)")

    if finds:
        print(f"\n  Finds ({len(finds)}):")
        for r in finds:
            via = r.get("via", "?")
            for listing in (r.get("listings") or ["?"]):
                print(f"    {r.get('ts','')[:16]}  {r.get('event',''):28} "
                      f"via {via:7} {listing}")
        # The question that settled whether these were sold or merely held in
        # someone else's basket: does any listing id ever come back?
        ids = [i for r in finds for i in (r.get("listing_ids") or [])]
        repeats = {i for i in ids if ids.count(i) > 1}
        print(f"    ids seen: {len(ids)}, ever seen twice: "
              f"{len(repeats)}{'  <- a listing came back' if repeats else ''}")

    if holds:
        won = sum(1 for r in holds if r.get("secured"))
        print(f"\n  Hold attempts: {len(holds)}, secured {won}")
        for r in holds[-8:]:
            secs = r.get("seconds")
            timings = r.get("timings") or {}
            worst = max(timings.items(), key=lambda kv: kv[1]) if timings else None
            print(f"    {r.get('ts','')[:16]}  {r.get('event',''):28} "
                  f"{'HELD' if r.get('secured') else 'lost'}"
                  f"{f'  {secs}s' if secs else ''}"
                  f"{f'  slowest {worst[0]} {worst[1]:.1f}s' if worst else ''}")
            if not r.get("secured") and r.get("reason"):
                print(f"        {str(r['reason'])[:90]}")
    print()
    return 0


def cmd_backup(_args) -> int:
    """Copy the files that live outside the repo and cannot be recreated."""
    from . import backup as backup_mod

    _banner("Backing up the runtime directory")
    result = backup_mod.run()
    print(backup_mod.describe(result))
    if result["error"]:
        return 1
    # Stamp the same clock the watch loop reads. A snapshot is a snapshot
    # whoever asked for it, and without this a backup taken by hand is
    # followed by an automatic one minutes later — which is what happened the
    # first time this ran.
    st = state_mod.load()
    state_mod.note_backup(st)
    state_mod.save(st)
    print(
        "\n  These restore on THIS Mac, under this user, only: macOS keeps the\n"
        "  key that decrypts Chrome's cookies in the login Keychain rather than\n"
        "  in the profile. That covers a bad profile reset or a mistaken rm,\n"
        "  which is what this is for — it is not a way to move the session to\n"
        "  another machine.\n"
    )
    return 0


def _maybe_backup() -> None:
    """Take a daily snapshot from inside the watch loop.

    Here rather than in a separate scheduled job because the watcher is the
    thing that is definitely running: another LaunchAgent is another thing to
    install, another thing to notice has been unloaded, and another thing that
    is silently not happening. This one cannot be forgotten while the watcher
    itself is alive.

    Every failure is swallowed. Nothing in a backup is worth costing a poll.
    """
    try:
        from . import backup as backup_mod

        st = state_mod.load()
        if not state_mod.backup_is_due(st):
            return
        result = backup_mod.run()
        print(backup_mod.describe(result))
        # Stamped even on failure, so a permanently broken backup cannot turn
        # into a retry on every poll — it says so once a day instead, which is
        # loud enough to notice and quiet enough to ignore while a ticket is
        # the priority.
        state_mod.note_backup(st)
        state_mod.save(st)
    except Exception as exc:
        print(f"[{stamp()}] backup skipped ({type(exc).__name__}: {exc})")


def cmd_test(_args) -> int:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        print("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.")
        return 1
    print(f"[{stamp()}] Sending test notifications to {config.ALERT_TO}...")
    try:
        notify.test()
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1
    print("Sent — check your inbox and phone.")
    return 0


def cmd_selftest(_args) -> int:
    """Run the offline checks: no network, no credentials, nothing sent.

    Covers the alert-gating rules and the content of every email — that each
    one goes to the right address and carries the link to the page you need to
    open. Safe to run any time, including while the watcher is running.
    """
    import subprocess

    tests = sorted((config.REPO_DIR / "tests").glob("test_*.py"))
    if not tests:
        print("No tests found.")
        return 1

    worst = 0
    for path in tests:
        print(f"\n{'=' * 68}\n  {path.name}\n{'=' * 68}")
        # Same warning filter the service runs with, so a real traceback is
        # not buried under a NotOpenSSLWarning from every import.
        env = dict(os.environ, PYTHONWARNINGS=os.environ.get("PYTHONWARNINGS", "ignore::Warning"))
        result = subprocess.run(
            [sys.executable, str(path)], cwd=str(config.REPO_DIR), env=env
        )
        worst = max(worst, result.returncode)
    print(f"\n{'ALL SUITES PASSED' if worst == 0 else 'SOME SUITES FAILED'}\n")
    return worst


def cmd_calibrate(_args) -> int:
    """Dump what the browser actually sees, for checking the text anchors.

    The anchors in sources/browser.py were written from a description of the
    page, since it can't be fetched without a logged-in browser. Run this once
    after `login` and read the .txt to confirm they match reality.
    """
    _banner("Dumping page diagnostics (this performs one search)")
    config.OFFSCREEN = False
    with _browser().BrowserSession(headless=False) as session:
        base = session.diagnose("calibrate")
    print(f"  Screenshot : {base.with_suffix('.png')}")
    print(f"  Visible text: {base.with_suffix('.txt')}   <- check the anchors against this")
    print(f"  Raw HTML   : {base.with_suffix('.html')}\n")
    return 0


def cmd_resolve_id(_args) -> int:
    """Find the id Discovery knows this event by, and check the resale path.

    Two questions at once. First, whether the id from the ticketmaster.ie URL
    works against Discovery directly. Second — the one that decides whether
    this can run off the Mac at all — whether resale inventory shows up as
    tmr-sourced events, since that is the only free, browser-free resale
    signal available.
    """
    if not discovery.configured():
        print("\n  Set TM_DISCOVERY_KEY first. Free, instant, no approval needed:")
        print("  https://developer.ticketmaster.com/  → sign up → copy the Consumer Key\n")
        return 1

    print(f"\n  Looking up {config.TM_EVENT_ID} directly...")
    try:
        direct = discovery._get(f"/events/{config.TM_EVENT_ID}.json")
    except Exception as exc:
        print(f"  direct lookup failed: {exc}")
        direct = None
    if direct:
        status = (direct.get("dates", {}).get("status", {}) or {}).get("code")
        print(f"  FOUND: {direct.get('name')}  [{status}] — the URL id works as-is.")
    else:
        print("  Not found by that id — use one from the search below.")

    try:
        events = discovery.search_events()
    except Exception as exc:
        print(f"  Search failed: {exc}")
        return 1

    print(f"\n  Electric Picnic events in Discovery (IE): {len(events)}")
    for e in events:
        print(f"    id={e['id']}  {e['date']}  [{e['status']}]  {e['name']}")

    print("\n  Ticketmaster Resale (source=tmr) events:")
    try:
        resale = discovery.find_resale_events()
    except Exception as exc:
        print(f"    lookup failed: {exc}")
        return 1
    if resale:
        for e in resale:
            print(f"    id={e['id']}  {e['date']}  {e['name']}  {e.get('price') or ''}")
        print("\n  Resale IS visible via the free API — browser-free hosting can watch it.")
    else:
        print("    (none)")
        print("\n  No tmr events right now. That is either genuinely no resale, or")
        print("  resale for this event never surfaces in Discovery. Re-run this when")
        print("  the browser reports a live resale listing — if this still says none")
        print("  at that moment, the free API cannot see resale and hosting needs a browser.")
    return 0


def cmd_check_mac(_args) -> int:
    """Has the Mac watcher checked in recently? Run from GitHub, not the Mac.

    The one failure no local safeguard can cover: if the laptop is off, every
    watchdog on it is off too, and the emails simply stop. Something outside
    the Mac has to notice, and the only thing that qualifies is the hourly
    Actions job.
    """
    from . import liveness

    if not liveness.topic():
        print("  No NTFY_TOPIC configured — cannot check the Mac's heartbeat.")
        return 0

    age = liveness.age_seconds()
    limit_h = config.MAC_SILENT_HOURS

    if age is None:
        # Deliberately not an alarm. No heartbeat within the cache window is
        # also what an ntfy outage looks like, and crying wolf here would
        # teach David to ignore the alert that says his watcher is really down.
        print(f"  No heartbeat found in the last 12h for topic {liveness.topic()}.")
        print("  Cannot distinguish 'Mac is off' from 'ntfy unreachable' — not alerting.")
        return 0

    hours = age / 3600.0
    print(f"  Mac last checked in {hours:.2f}h ago (limit {limit_h}h).")

    st = state_mod.load()
    # Which silence is this? Identified by when the last beacon actually
    # arrived, not by how stale it is — the staleness grows every hour while
    # the silence stays the same one.
    beacon_at = (state_mod.utc_now() - datetime.timedelta(seconds=age)).replace(
        microsecond=0, second=0)

    if hours < limit_h:
        print("  Mac watcher is alive.")
        if st.get("mac_silent_alerted_at"):
            print("  ...and it was previously reported quiet — clearing that.")
            st["mac_silent_alerted_at"] = None
            st["mac_silent_beacon_at"] = None
            state_mod.save(st)
        return 0

    # Repeat suppression. Without it this fired every hour about a heartbeat
    # that had not moved — and on 2026-08-19 the heartbeat had not moved
    # because ntfy was rate-limiting the Mac, so the alert was false as well
    # as repetitive.
    same_silence = st.get("mac_silent_beacon_at") == beacon_at.isoformat()
    since = state_mod._hours_since(st.get("mac_silent_alerted_at"))
    if same_silence and since is not None and since < config.MAC_SILENT_RENAG_HOURS:
        print(f"  Already reported this silence {since:.1f}h ago; next repeat "
              f"after {config.MAC_SILENT_RENAG_HOURS:.0f}h. Not alerting again.")
        return 1

    print("  Mac watcher looks DOWN — alerting.")
    notify.mac_watcher_silent(hours, repeat=same_silence)
    st["mac_silent_alerted_at"] = state_mod.utc_now().isoformat()
    st["mac_silent_beacon_at"] = beacon_at.isoformat()
    state_mod.save(st)
    return 1


def cmd_doctor(_args) -> int:
    """Check everything, and for anything wrong print the exact fix to paste.

    Written for the moment something has gone quiet and you want one command
    that says whether it is working, rather than four commands and a guess.
    Every failure line carries the command that repairs it — a diagnosis you
    have to go and look up is half a diagnosis.
    """
    import subprocess

    print(f"\n  EP2026 watcher — health check at {stamp()}\n")
    problems = []
    warnings = []

    def ok(label, detail=""):
        print(f"  [ OK ]  {label}{'  — ' + detail if detail else ''}")

    def bad(label, detail, fix):
        print(f"  [FAIL]  {label}  — {detail}")
        problems.append((label, fix))

    def warn(label, detail):
        """Not broken, but not fine either — and it must reach the summary.

        These used to print and then vanish: the closing line counted only
        hard failures, so doctor could report "Everything is working. Nothing
        to do." directly beneath a warning that resale — the market a ticket
        has actually appeared on — was unreadable on a third of polls. A
        summary that contradicts its own body teaches you to stop reading it.
        """
        print(f"  [WARN]  {label}  — {detail}")
        warnings.append((label, detail))

    # 1. Is the service installed and running?
    plist = Path.home() / "Library/LaunchAgents/com.davidcoyne.ep2026watcher.plist"
    label = "com.davidcoyne.ep2026watcher"
    reinstall = (
        f"cd {config.REPO_DIR} && cp launchd/{plist.name} ~/Library/LaunchAgents/ && "
        f"launchctl load ~/Library/LaunchAgents/{plist.name}"
    )
    if not plist.exists():
        bad("LaunchAgent installed", "plist not in ~/Library/LaunchAgents", reinstall)
    else:
        ok("LaunchAgent installed")

    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    running_pid = None
    for line in listed.stdout.splitlines():
        if label in line:
            parts = line.split()
            running_pid = parts[0] if parts[0] != "-" else None
    if running_pid:
        ok("Service running", f"pid {running_pid}")
    else:
        bad("Service running", "not loaded, or loaded but not started", reinstall)

    # Nothing watches the watchdog. If it gets unloaded, hang-detection is
    # silently gone and everything still looks fine — so check it explicitly.
    if "com.davidcoyne.ep2026watchdog" in listed.stdout:
        ok("Watchdog loaded", "hang detection active")
    else:
        bad("Watchdog loaded", "nothing would restart a hung watcher",
            f"{config.REPO_DIR}/restart.sh")

    # 2. Is it actually doing work, or merely alive? A hung process passes
    #    every check above and does nothing at all.
    st = state_mod.load()
    # Night-aware, because it was not and that made it lie every night.
    # The threshold was derived from the daytime cycle alone, so once the
    # overnight slowdown kicked in — a 30-minute interval, jittered to as
    # much as 38 — every ordinary gap exceeded it and doctor reported a
    # perfectly healthy watcher as wedged. Measured on the night of
    # 2026-08-16: gaps reached 36 minutes against a 30-minute threshold.
    interval, night = config.poll_interval_now()
    stale_after = max(interval * 2, 900) / 3600.0
    cadence = f"{interval // 60} min cadence{' overnight' if night else ''}"

    age = state_mod.hours_since_check(st)
    resting = state_mod.backoff_remaining(st)
    holding = state_mod.hold_remaining(st)
    # Before every other verdict about the poll clock. A watcher holding a
    # basket has stopped on purpose, and the fix printed for "looks wedged" is
    # a kickstart — which would kill the browser the ticket is sitting in.
    if holding:
        print(f"  [ !! ]  A TICKET IS HELD — {holding / 60:.0f} min left on the hold")
        print("          Finish the checkout in the Chrome window that is open.")
        print("          Nothing will restart the watcher until that runs out.")
    if age is None:
        bad("Polling", "no check has ever been recorded",
            f"tail -20 {config.LOG_DIR}/watcher.log")
    elif holding:
        # Not polling because a ticket is held is the one case where stopping
        # is the correct behaviour, so it must not read as a fault.
        ok("Polling", f"paused while a ticket is held — resumes in {holding / 60:.0f} min")
    elif resting:
        # Deliberately waiting out a rate limit is not being wedged, and the
        # two must not look alike: restarting during a backoff is how a short
        # block becomes a long one.
        ok("Polling", f"backing off after a 403 — resumes in {resting / 60:.0f} min")
    elif age > stale_after:
        bad("Polling",
            f"last check was {age * 60:.0f} min ago, over the "
            f"{stale_after * 60:.0f} min limit for a {cadence} — looks wedged",
            f"launchctl kickstart -k gui/$(id -u)/{label}")
    else:
        ok("Polling", f"last check {age * 60:.0f} min ago ({cadence})")

    # 2b. Running is not the same as seeing. A poll can succeed, report a
    #     confident no on primary, and have learned nothing about resale
    #     because the search resolved before the resale panel rendered. That
    #     is the market a ticket actually appears on, so it gets its own line
    #     rather than being averaged into "polling works".
    severity, headline = state_mod.resale_visibility(st)
    if severity == "bad":
        bad("Resale visibility", headline,
            f"{config.REPO_DIR}/run_watcher.sh calibrate   # dump what the page renders")
    elif severity == "watch":
        warn("Resale visibility", headline)
    elif severity == "unknown":
        print(f"  [ -- ]  Resale visibility  — {headline}")
    else:
        ok("Resale visibility", headline)

    # 2c. Securing is armed but the session behind it can rot silently.
    #     Cookies expire, Ticketmaster invalidates them, a profile reset wipes
    #     them — and all three first show up as a listing appearing and not
    #     being held. This is the cheap half of the check: the profile exists
    #     at all. Whether the session inside it is still valid needs a browser,
    #     which is what `check-buy` is for.
    if config.SECURE_ON_FIND:
        from . import buyer

        login_fix = f"{config.REPO_DIR}/run_watcher.sh login-buy"
        ev = buyer.session_evidence(config.BUY_PROFILE_DIR)
        if ev["signed_in"] is False:
            bad("Securing", f"armed, but {ev['reason']}", login_fix)
        elif ev["signed_in"] is None:
            warn("Securing", f"armed, but {ev['reason']}")
        else:
            ok("Securing", f"armed and signed in — {ev['reason']}")
            # The session expiring is the failure this is really watching
            # for: it is silent, and its first symptom would be a listing
            # appearing and not being held.
            # Deliberately not a warning. See cmd_check_buy: the expiry of
            # any one cookie is not the expiry of the session, and warning on
            # it produced "0.1 days" on a perfectly healthy profile — the kind
            # of alarm that teaches you to ignore alarms.
            days = ev["days_left"]
            if days is not None:
                print(f"  [ -- ]  Buying session  — first account cookie lapses "
                      f"{buyer.describe_lapse(days)}; presence is the real check")
    else:
        print("  [ -- ]  Securing  — off; the watcher only notifies")

    age = state_mod.profile_age_minutes(st)
    if not config.PROFILE_MAX_AGE_MINUTES:
        print("  [ -- ]  Browser identity refresh disabled (EP_PROFILE_MAX_AGE=0)")
    elif age is None:
        print("  [ -- ]  Browser identity  — age not recorded yet (starts on the next run)")
    elif state_mod.profile_is_stale(st):
        warn("Browser identity", f"{age:.0f} min old — due a refresh on the next poll")
    else:
        ok("Browser identity",
           f"{age:.0f} min old (refreshed every {config.PROFILE_MAX_AGE_MINUTES:.0f})")

    partial = st.get("degraded_total", 0)
    if partial:
        print(f"          ({partial} poll(s) answered by only some sources)")

    # 3. Can it tell you anything?
    # Actually sign in, rather than noting that a password is set. A revoked
    # app password looks identical from here, and the first thing to discover
    # it would be the one alert that mattered failing to arrive.
    mail_ok, mail_detail = notify.verify_email()
    if mail_ok:
        ok("Email delivery", f"{mail_detail} → {config.ALERT_TO}")
    else:
        bad("Email delivery", mail_detail,
            f"edit {Path.home()}/.ep2026-watcher/env, then ./run_watcher.sh test")
    # Actually exercise push rather than just noting a topic is set — a
    # configured topic that nothing reaches is the failure that matters, and
    # it looks identical to a working one from here.
    push_ok, push_detail = notify.verify_push()
    if push_ok:
        ok("Push delivery", push_detail)
        print("          (proves ntfy works; only your phone can prove it is subscribed)")
    elif "429" in push_detail:
        # Not a fault to go and fix — a quota to stop spending. It clears by
        # itself, and the thing that exhausted it (the liveness beacon) is now
        # throttled. Reporting it as a broken topic sent David to edit a
        # setting that was correct.
        warn("Push delivery", push_detail)
        print("          Email still works. The beacon is throttled so this "
              "should not recur.")
    elif config.NTFY_TOPIC:
        bad("Push delivery", push_detail, "check NTFY_TOPIC in ~/.ep2026-watcher/env")
    else:
        print("  [ -- ]  Push not configured — email only, which is minutes slower")

    # The phone call. Optional, so its absence is a dash rather than a fault —
    # but a number that is SET and malformed is a real problem, because
    # everything looks configured right up until Twilio refuses the call, and
    # the moment that happens is the moment a ticket is on screen.
    phone_fault = config.phone_problem(config.ALERT_PHONE)
    from_fault = config.phone_problem(config.TWILIO_FROM)
    if config.can_ring_phone():
        ok("Phone call", f"will ring {config.ALERT_PHONE} on a real find")
    elif config.ALERT_PHONE and phone_fault:
        bad("Phone call", f"ALERT_PHONE {phone_fault}",
            "fix ALERT_PHONE in ~/.ep2026-watcher/env, then: "
            "python -m ep_watcher ring")
    elif config.TWILIO_FROM and from_fault:
        bad("Phone call", f"TWILIO_FROM {from_fault}",
            "fix TWILIO_FROM in ~/.ep2026-watcher/env, then: "
            "python -m ep_watcher ring")
    elif config.ALERT_PHONE:
        # Half-configured is worth naming rather than dashing: somebody
        # started setting this up and stopped, and will otherwise assume the
        # phone rings.
        short = [n for n in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM")
                 if not getattr(config, n)]
        warn("Phone call",
             f"a number is set but the phone cannot ring yet "
             f"(no {', '.join(short)})")
        print("          Optional. `python -m ep_watcher ring` says what is "
              "needed.")
    else:
        print("  [ -- ]  Phone call not configured — see `ring` (optional)")

    # The number that decides whether push works for the rest of the day.
    # Nothing counted it until 2026-08-19, which is how the allowance was
    # spent invisibly and the channel a ticket alert travels on stayed dead
    # for five hours while every local check reported healthy.
    if config.NTFY_TOPIC:
        from . import pushquota

        left = pushquota.remaining()
        if left <= 0:
            # A warning, not a failure. Nothing here can be run: the allowance
            # resets on its own and email is unaffected, so listing it under
            # "Fixes, in order" would put a line with no command under a
            # heading that promises one.
            warn("Push quota",
                 f"{pushquota.summary()} — no more push today. It resets "
                 f"daily; email alerts are unaffected")
        elif left <= config.NTFY_ALERT_RESERVE:
            warn("Push quota",
                 f"{pushquota.summary()} — the heartbeat has stood down to "
                 f"keep what is left for alerts")
        else:
            ok("Push quota", pushquota.summary())

    # Is the off-Mac dead man's switch actually armed? It is what covers the
    # laptop being off, so it failing quietly would remove the last line of
    # defence without any visible change.
    from . import liveness

    if liveness.topic():
        age = liveness.age_seconds()
        if age is None:
            print("  [ -- ]  Remote heartbeat  — none published yet (starts on the next poll)")
        elif age / 3600.0 < config.MAC_SILENT_HOURS:
            ok("Remote heartbeat", f"last {age / 60:.0f} min ago — GitHub can see this Mac")
        elif "429" in push_detail:
            # The heartbeat travels over the same ntfy quota the check above
            # just found exhausted, so a stale beacon here is a symptom of
            # that and not a separate fault. Reporting it as a failure whose
            # fix is restart.sh would send David to bounce a perfectly healthy
            # watcher — which is precisely what tonight's false alarm invited,
            # and restarting would not have published a single beacon.
            warn("Remote heartbeat", f"stale ({age / 3600.0:.1f}h) — because ntfy "
                                     f"is rate-limiting, not because the Mac is down")
            print("          It recovers when the quota does. Until then the "
                  "GitHub backstop may")
            print("          email 'your Mac watcher has gone quiet' while the "
                  "watcher is fine.")
        else:
            bad("Remote heartbeat", f"stale ({age / 3600.0:.1f}h)",
                f"{config.REPO_DIR}/restart.sh")
    else:
        print("  [ -- ]  Remote heartbeat not configured (needs NTFY_TOPIC)")

    # 4. Is the connection healthy? Report the real severity: printing [ OK ]
    #    next to the words "being rate-limited" is contradictory, and a health
    #    check people learn to squint at is not a health check.
    print(f"  [ -- ]  On {state_mod.describe_network(st)}"
          + (f" — {state_mod.naming_key(st)}" if not state_mod.is_named(st) else ""))
    severity, headline, _ = state_mod.connection_health(st)
    if severity == "blocked":
        bad("Connection", headline, "switch networks; see the email for the full steps")
    elif severity == "watch":
        warn("Connection", headline)
    else:
        ok("Connection", headline)

    # 5. Will the Mac stay awake long enough to matter?
    # pmset pads its columns with a variable number of tabs — "SleepDisabled"
    # is followed by two, not one. Matching a literal \t reported this as
    # broken while it was correctly set, and a health check that cries wolf
    # is worse than no health check.
    import re

    sleep_cfg = subprocess.run(["pmset", "-g"], capture_output=True, text=True).stdout
    disabled = re.search(r"SleepDisabled\s+1\b", sleep_cfg)
    never_sleeps = re.search(r"^\s*sleep\s+0\b", sleep_cfg, re.MULTILINE)
    prevented = "sleep prevented" in sleep_cfg
    if disabled or never_sleeps or prevented:
        ok("Mac stays awake", "sleep disabled")
    else:
        bad("Mac stays awake", "it may sleep, which stops the watcher",
            "sudo pmset -a disablesleep 1")

    # 6. Has it retired?
    if state_mod.past_stop_date():
        print(f"\n  Note: past the stop date ({config.STOP_AFTER_DATE}) — "
              "the watcher has finished on purpose.")
        return 0

    print()
    summary, code = doctor_summary(problems, warnings)
    print(summary)
    return code


def doctor_summary(problems, warnings) -> tuple:
    """Render doctor's closing verdict. Returns (text, exit code).

    Split out so it can be checked directly. The rule it enforces is the one
    that was broken: a warning must reach the summary. doctor used to count
    only hard failures, so it printed "Everything is working. Nothing to do."
    underneath a WARN line saying resale — the market a ticket has actually
    appeared on — was unreadable on a third of polls. A summary that
    contradicts its own body is worse than no summary, because it is the line
    people read instead of the body.

    Warnings do not make the exit code non-zero: nothing here needs a command
    run, and the code is what the watchdog and the scripts branch on. But
    "nothing to run" is not "nothing to know".
    """
    if not problems and not warnings:
        return "  Everything is working. Nothing to do.\n", 0

    out = []
    if problems:
        out.append(f"  {len(problems)} problem(s). Fixes, in order:\n")
        for name, fix in problems:
            out.append(f"    # {name}")
            out.append(f"    {fix}\n")
        out.append(f"  Or just re-run everything:  {config.REPO_DIR}/restart.sh\n")

    if warnings:
        head = "Nothing is broken, but" if not problems else "Also"
        out.append(f"  {head} {len(warnings)} thing(s) worth an eye:\n")
        for name, detail in warnings:
            out.append(f"    · {name}: {detail}")
        out.append("")

    return "\n".join(out), (1 if problems else 0)


def cmd_networks(_args) -> int:
    """List every connection the watcher has seen, and how to name them.

    Written for the question David actually asks when a connection is flagged:
    not "is this one bad" but "which one should I go and buy on". That needs
    the whole list, with the block count against each, and a name he
    recognises — so this also prints the exact line to paste to name any of
    them.
    """
    from . import network

    st = state_mod.load()
    fp = network.fingerprint(max_age=0)
    if fp.get("key"):
        # Recorded, so a connection joined since the last poll shows up here
        # rather than only after the watcher next runs.
        state_mod.note_network(st, fp)
        state_mod.save(st)

    rows = state_mod.known_networks(st)
    if not rows:
        print("\n  No connections recorded yet — they are learned as the Mac joins them.\n")
        return 0

    print(f"\n  Connections this watcher knows, at {stamp()}\n")
    width = max(len(r[0]) for r in rows)
    print(f"    {'':2} {'Name':{width}} {'Searches':>9} {'Blocks':>7}   Key to name it by")
    print(f"    {'-' * (width + 42)}")
    for label, key, searches, blocks, is_current in rows:
        mark = "->" if is_current else "  "
        naming = state_mod.naming_key(st, key)
        print(f"    {mark} {label:{width}} {searches:>9} {blocks:>7}   {naming}")

    print(f"\n  -> is the connection in use now: {state_mod.describe_network(st)}")
    if fp.get("ip"):
        print(f"     Public address {fp['ip']}, which is what Ticketmaster sees.")

    # Only offer to name connections worth naming: the one in use, and any
    # that has drawn a block. The rest are addresses a tether held for twenty
    # minutes two days ago, and they are pruned on their own.
    unnamed = [
        (label, state_mod.naming_key(st, key))
        for label, key, _searches, blocks, is_current in rows
        if not state_mod.is_named(st, key) and (is_current or blocks)
    ]
    if unnamed:
        print("\n  The names above are the watcher's own guesses, which is fine — every")
        print("  connection is tracked and blamed correctly either way. To set them")
        print(f"  yourself, put this in {Path.home()}/.ep2026-watcher/env and restart:\n")
        pairs = ",".join(f"{key}=your name for it" for _label, key in unnamed)
        print(f'      EP_NETWORK_NAMES="{pairs}"\n')
    else:
        print("\n  All named explicitly.\n")
    return 0


def budget_report() -> tuple:
    """(lines, over_budget) describing what this cadence actually spends.

    Split from the command so the test suite can assert on it directly. The
    number that matters is the PEAK hour, not the daily total: a rate limit
    measures requests inside a window, so a day that averages comfortably can
    still be refused at 15:00.

    This exists because the arithmetic kept being done in comments. Three
    separate blocks in config.py claimed 12, 15.3 and 17 searches an hour for
    a configuration that really spent 18.5, each one accurate when written and
    none of them updated when a page's range moved. A number nothing computes
    is a number that drifts.
    """
    peak = config.peak_searches_per_hour()
    limit = config.BLOCK_RATE_PER_HOUR
    hourly = [config.searches_per_hour_at(h) for h in range(24)]
    quietest = min(hourly)
    peak_hours = [h for h, rate in enumerate(hourly) if abs(rate - peak) < 0.05]

    lines = []
    lines.append("")
    lines.append("  What this cadence actually spends")
    lines.append("")
    verdict = "OVER" if peak > limit else "under"
    lines.append(f"    Busiest hour   : {peak:5.1f} searches/hour  "
                 f"({verdict} the {limit:.0f}/hour that drew a block)")
    lines.append(f"    Quietest hour  : {quietest:5.1f} searches/hour")
    lines.append(f"    Whole day      : {sum(hourly):5.0f} searches")
    lines.append(f"    Loop tick      : every {config.POLL_INTERVAL_SECONDS}s "
                 f"— wakes to ask if a page is due; costs no requests")
    # The sweep is not a search and must not be added to the search rate — the
    # limit above was derived from searches, and quietly folding a different
    # kind of request into it would make both numbers meaningless. But it IS
    # request volume, and a budget report that omits a source of requests is
    # the kind of reassuring number this project exists to distrust.
    if config.RESALE_SWEEP:
        live = [e for e in config.EVENTS if e.searchable()]
        per_hour = 3600.0 / config.RESALE_SWEEP_SECONDS * len(live)
        lines.append(
            f"    Resale sweep   : {per_hour:5.1f} calls/hour  "
            f"(every {config.RESALE_SWEEP_SECONDS}s x {len(live)} pages)")
        lines.append(
            "                     one same-origin XHR each, from the page already")
        lines.append(
            "                     open — not a search, and not counted above.")
        lines.append(
            "                     The endpoint's own cache-control says max-age=15,")
        lines.append(
            "                     so it expects to be asked far more often than this.")
    else:
        lines.append("    Resale sweep   : off (EP_RESALE_SWEEP=0)")
    if peak_hours:
        span = f"{peak_hours[0]:02d}:00-{peak_hours[-1]:02d}:59 local"
        lines.append(f"    Busiest window : {span}")
    lines.append("")
    lines.append("  Per page, in minutes between searches:")
    for event in config.EVENTS:
        # A page that is off is listed, not omitted. Omitting it would make a
        # switched-off page and a forgotten one look identical here, which is
        # the one thing this report exists not to do — and this is the report
        # somebody reads when asking "why is nothing happening on that page?".
        if not event.searchable():
            why = ("past its stop date" if event.expired()
                   else "switched off — see EP_EARLY_ENTRY in config.py")
            lines.append(f"    {event.slug:28} NOT SEARCHED  ({why})")
            continue
        peak_lo, peak_hi = event.gap_range(datetime.datetime(2000, 1, 1, 12, 30))
        off_lo, off_hi = event.gap_range(datetime.datetime(2000, 1, 1, 22, 30))
        rate = 3600.0 / ((peak_lo + peak_hi) / 2.0)
        secured = "holds" if event.secure else "alerts only"
        lines.append(
            f"    {event.slug:28} peak {peak_lo // 60:2.0f}-{peak_hi // 60:<2.0f} "
            f"off-peak {off_lo // 60:2.0f}-{off_hi // 60:<3.0f} "
            f"= {rate:4.1f}/hr at peak   ({secured})"
        )
    lines.append("")
    lines.append("  By hour of the local clock:")
    scale = max(hourly) or 1.0
    for hour in range(24):
        bar = "#" * int(round(hourly[hour] / scale * 28))
        marker = "  <- peak" if hour in peak_hours else ""
        lines.append(f"    {hour:02d}  {hourly[hour]:5.1f}  {bar}{marker}")
    lines.append("")

    if peak > limit:
        lines.append(f"  OVER BUDGET. The busiest hour sends {peak:.1f} searches, above the")
        lines.append(f"  {limit:.0f}/hour that got this client answered with HTTP 403 on")
        lines.append("  2026-08-13. Raise a page's EP_*_PEAK_MIN / _MAX to slow it down.")
    else:
        headroom = limit - peak
        lines.append(f"  Under budget, with {headroom:.1f} searches/hour of headroom.")
        lines.append("  That is not a safe margin, it is an unbroken one: the real")
        lines.append("  threshold is unpublished, and the watcher was blocked again on")
        lines.append("  2026-08-19 at 05:43 while running below this rate.")
    lines.append("")
    return lines, peak > limit


def cmd_budget(_args) -> int:
    """Print the request budget. Non-zero exit if it is over the block line."""
    _banner("Request budget")
    lines, over = budget_report()
    print("\n".join(lines))
    return 1 if over else 0


def cmd_ring(_args) -> int:
    """Place a real test call, so the setup is proven before a ticket needs it.

    The whole point of the phone channel is that it works at 3am when nothing
    else does, and there is exactly one bad moment to discover a wrong number
    or a lapsed Twilio trial. So this rings for real rather than validating
    credentials — a call that Twilio accepts and that never reaches the
    handset is the failure worth catching, and only an actual ring finds it.
    """
    _banner("Ringing your phone")
    if not config.can_ring_phone():
        missing = [n for n in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM",
                               "ALERT_PHONE") if not getattr(config, n)]
        print("  Phone calls are OFF — not configured.\n")
        if missing:
            print(f"  Missing: {', '.join(missing)}\n")
        # A number that is set but malformed is the worse case: everything
        # looks configured and Twilio refuses the call at the one moment it
        # matters. Say exactly what is wrong with it.
        for label, number in (("ALERT_PHONE", config.ALERT_PHONE),
                              ("TWILIO_FROM", config.TWILIO_FROM)):
            problem = config.phone_problem(number)
            if problem and number:
                print(f"  {label} looks wrong: {problem}\n")
        print("  Everything else still works; this is an optional extra.\n")
        # Only what is actually absent. Listing settings he has already put in
        # reads as though they did not take, which is the wrong thing to think
        # while looking at a file you just edited.
        hints = {
            "TWILIO_SID": "TWILIO_SID=AC...            # Twilio console",
            "TWILIO_TOKEN": "TWILIO_TOKEN=...             # Twilio console",
            "TWILIO_FROM": "TWILIO_FROM=+353...          # your Twilio number",
            "ALERT_PHONE": "ALERT_PHONE=+353...          # your mobile",
        }
        if missing:
            print("  Add to ~/.ep2026-watcher/env (no quotes, no 'export' —")
            print("  run_watcher.sh sources it with `set -a`):\n")
            for name in missing:
                print(f"      {hints[name]}")
            print()
        already = [n for n in hints if n not in missing]
        if already:
            print(f"  Already set: {', '.join(already)}\n")
        print("  A Twilio number is about €1/month and a call about €0.02.")
        print("  Then run this again.\n")
        return 1

    print(f"  Calling {config.ALERT_PHONE} from {config.TWILIO_FROM} ...\n")
    # Bypass the cooldown: a test the user asked for must not be silently
    # swallowed because a real alert happened to ring nine minutes ago.
    notify._last_call_at = 0.0
    if notify.ring_phone("This is a test of the Electric Picnic ticket watcher"):
        print("  Placed. Your phone should ring within a few seconds.\n")
        print("  If it does not, the number or the Twilio account is the")
        print("  problem — check the Twilio console's call log.\n")
        return 0
    print("\n  The call was NOT placed. The reason is above.\n")
    return 1


def cmd_status(_args) -> int:
    st = state_mod.load()
    print(f"\n  State file : {config.STATE_FILE}")
    print(f"  Profile    : {config.PROFILE_DIR}  (exists: {config.PROFILE_DIR.exists()})")
    print(f"  Press mode : {config.PRESS_THE_BUTTON}")
    print(f"  Browser    : {'enabled' if config.USE_BROWSER else 'DISABLED (API-only mode)'}")
    print(f"  Discovery API configured: {discovery.configured()}")
    print(f"  Inventory API configured: {inventory_api.configured()}")
    print(f"  Email configured        : {bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)}")
    print(f"  Push configured         : {bool(config.NTFY_TOPIC)}")
    # The one number that decides whether this watcher keeps working at all.
    # Summarised here and explained in full by `budget`.
    holding = state_mod.hold_remaining(st)
    if holding:
        print(f"  A TICKET IS HELD        : {holding / 60:.0f} min left — "
              f"finish it in the open Chrome window")
    peak = config.peak_searches_per_hour()
    print(f"  Peak request rate       : {peak:.1f}/hour of "
          f"{config.BLOCK_RATE_PER_HOUR:.0f} "
          f"({'OVER — see `budget`' if peak > config.BLOCK_RATE_PER_HOUR else 'ok'})\n")
    print(json.dumps(st, indent=2))
    healthy = st["consecutive_failures"] < config.WATCHDOG_FAILURE_THRESHOLD
    print(f"\n  Health: {'OK' if healthy else 'BROKEN — check the logs'}\n")
    return 0 if healthy else 1


COMMANDS = {
    "login": cmd_login,
    "login-buy": cmd_login_buy,
    "check-buy": cmd_check_buy,
    "login-auto": cmd_login_auto,
    "check": cmd_check,
    "run": cmd_run,
    "watch": cmd_watch,
    "test": cmd_test,
    "selftest": cmd_selftest,
    "doctor": cmd_doctor,
    "check-mac": cmd_check_mac,
    "calibrate": cmd_calibrate,
    "resolve-id": cmd_resolve_id,
    "networks": cmd_networks,
    "status": cmd_status,
    "ring": cmd_ring,
    "budget": cmd_budget,
    "backup": cmd_backup,
    "events": cmd_events,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ep_watcher",
        description="Electric Picnic 2026 ticket watcher.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="what to do")
    parser.add_argument(
        "--interval", type=int, default=None,
        help="seconds between polls in `watch` (default %(default)s)",
    )
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
