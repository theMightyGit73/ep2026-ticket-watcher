"""Command line entry point:  python -m ep_watcher <command>"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

from . import config, engine, notify, state as state_mod
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
        print(f"\n  Signed in. Buying session saved to {config.BUY_PROFILE_DIR}")
        print("  Turn the feature on with EP_SECURE_ON_FIND=1 when you want it.\n")
        return 0
    print("\n  Could not confirm a signed-in session — run `login-buy` again.\n")
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

    if reading.needs_login:
        print("\n  Session needs attention:  python -m ep_watcher login\n")
    return 1 if reading.failed else 0


def cmd_run(_args) -> int:
    """One full cycle including alerts. This is what a scheduler calls."""
    if _stop_if_past_date():
        return 0
    reading = engine.run_once()
    return 1 if reading.failed else 0


def cmd_watch(args) -> int:
    """Long-running loop holding one warm browser open between polls.

    Preferred over scheduling one-shot `run`s: the session stays warm, each
    poll costs a page load instead of a browser cold start, and a persistent
    real browser is a far more ordinary thing to be doing than a fresh
    headless Chrome every two minutes.
    """
    interval = args.interval or config.POLL_INTERVAL_SECONDS
    if config.PRESS_THE_BUTTON and interval < config.PRESS_MIN_INTERVAL_SECONDS:
        print(
            f"[{stamp()}] press mode: raising interval {interval}s → "
            f"{config.PRESS_MIN_INTERVAL_SECONDS}s (each poll is a real reserve attempt)"
        )
        interval = config.PRESS_MIN_INTERVAL_SECONDS

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
    if config.SECURE_ON_FIND:
        signed_in = (config.BUY_PROFILE_DIR / "Default" / "Cookies").exists()
        print(f"  Securing: ON — will hold a resale listing, never pay for it")
        if not signed_in:
            print(f"    ⚠ the buying profile is NOT signed in, so securing")
            print(f"      cannot work yet. Run:  python -m ep_watcher login-buy")
    else:
        print("  Securing: off — notify only (EP_SECURE_ON_FIND=1 to enable)")

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
                    print(f"[{stamp()}] Reserve accepted — pausing the loop so you can check out.")
                    print("  The browser is holding the basket. Ctrl-C when you're done.")
                    while True:
                        time.sleep(30)
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
            _refresh_profile_if_stale(session)
            time.sleep(sleeping)
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] Stopped.")
        return 0
    finally:
        session.close()


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
    if hours < limit_h:
        print("  Mac watcher is alive.")
        return 0

    print("  Mac watcher looks DOWN — alerting.")
    notify.mac_watcher_silent(hours)
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
    if age is None:
        bad("Polling", "no check has ever been recorded",
            f"tail -20 {config.LOG_DIR}/watcher.log")
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
    elif config.NTFY_TOPIC:
        bad("Push delivery", push_detail, "check NTFY_TOPIC in ~/.ep2026-watcher/env")
    else:
        print("  [ -- ]  Push not configured — email only, which is minutes slower")

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


def cmd_status(_args) -> int:
    st = state_mod.load()
    print(f"\n  State file : {config.STATE_FILE}")
    print(f"  Profile    : {config.PROFILE_DIR}  (exists: {config.PROFILE_DIR.exists()})")
    print(f"  Press mode : {config.PRESS_THE_BUTTON}")
    print(f"  Browser    : {'enabled' if config.USE_BROWSER else 'DISABLED (API-only mode)'}")
    print(f"  Discovery API configured: {discovery.configured()}")
    print(f"  Inventory API configured: {inventory_api.configured()}")
    print(f"  Email configured        : {bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)}")
    print(f"  Push configured         : {bool(config.NTFY_TOPIC)}\n")
    print(json.dumps(st, indent=2))
    healthy = st["consecutive_failures"] < config.WATCHDOG_FAILURE_THRESHOLD
    print(f"\n  Health: {'OK' if healthy else 'BROKEN — check the logs'}\n")
    return 0 if healthy else 1


COMMANDS = {
    "login": cmd_login,
    "login-buy": cmd_login_buy,
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
