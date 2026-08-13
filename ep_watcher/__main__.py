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


def cmd_check(_args) -> int:
    """Read once and print the result. Sends nothing."""
    _banner("Manual check (no notifications)")
    reading = engine.poll()

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
    if discovery.configured():
        print("  Discovery API: configured")
    if inventory_api.configured():
        print("  Inventory Status API: configured")

    if not config.USE_BROWSER:
        # API-only: no browser to keep warm, so this is just a polling loop.
        print("  Browser DISABLED — API sources only\n")
        return _watch_apis_only(interval)

    session = _browser().BrowserSession()
    session.start()
    backoff = 0
    tried_profile_reset = False
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
                        tried_profile_reset = True
                        time.sleep(30)
                        continue
                    backoff = min(backoff * 2 or config.BLOCKED_BACKOFF_SECONDS,
                                  config.BLOCKED_BACKOFF_MAX_SECONDS)
                    print(
                        f"[{stamp()}] still blocked with a fresh profile — this is the "
                        f"network. Sleeping {backoff // 60} min."
                    )
                    time.sleep(backoff * random.uniform(0.85, 1.15))
                    continue
                if backoff or tried_profile_reset:
                    print(f"[{stamp()}] recovered from rate limiting")
                    backoff = 0
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

            sleep_for = interval * random.uniform(0.75, 1.25)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] Stopped.")
        return 0
    finally:
        session.close()


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
        result = subprocess.run([sys.executable, str(path)], cwd=str(config.REPO_DIR))
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

    def ok(label, detail=""):
        print(f"  [ OK ]  {label}{'  — ' + detail if detail else ''}")

    def bad(label, detail, fix):
        print(f"  [FAIL]  {label}  — {detail}")
        problems.append((label, fix))

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

    # 2. Is it actually doing work, or merely alive? A hung process passes
    #    every check above and does nothing at all.
    st = state_mod.load()
    stale_after = max(config.POLL_INTERVAL_SECONDS * 3, 900) / 3600.0
    age = state_mod.hours_since_check(st)
    if age is None:
        bad("Polling", "no check has ever been recorded",
            f"tail -20 {config.LOG_DIR}/watcher.log")
    elif age > stale_after:
        bad("Polling", f"last check was {age * 60:.0f} min ago — looks wedged",
            f"launchctl kickstart -k gui/$(id -u)/{label}")
    else:
        ok("Polling", f"last check {age * 60:.0f} min ago")

    # 3. Can it tell you anything?
    if config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD:
        ok("Email configured", config.ALERT_TO)
    else:
        bad("Email configured", "no Gmail app password",
            f"edit {Path.home()}/.ep2026-watcher/env, then ./run_watcher.sh test")
    if config.NTFY_TOPIC:
        ok("Push configured")
    else:
        print("  [ -- ]  Push not configured (optional, but faster than email)")

    # 4. Is the connection healthy?
    severity, headline, _ = state_mod.connection_health(st)
    if severity == "blocked":
        bad("Connection", headline, "switch networks; see the email for the full steps")
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
    if not problems:
        print("  Everything is working. Nothing to do.\n")
        return 0

    print(f"  {len(problems)} problem(s). Fixes, in order:\n")
    for name, fix in problems:
        print(f"    # {name}")
        print(f"    {fix}\n")
    print(f"  Or just re-run everything:  {config.REPO_DIR}/restart.sh\n")
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
    print(f"  Push configured         : {bool(config.NTFY_TOPIC)}\n")
    print(json.dumps(st, indent=2))
    healthy = st["consecutive_failures"] < config.WATCHDOG_FAILURE_THRESHOLD
    print(f"\n  Health: {'OK' if healthy else 'BROKEN — check the logs'}\n")
    return 0 if healthy else 1


COMMANDS = {
    "login": cmd_login,
    "check": cmd_check,
    "run": cmd_run,
    "watch": cmd_watch,
    "test": cmd_test,
    "selftest": cmd_selftest,
    "doctor": cmd_doctor,
    "calibrate": cmd_calibrate,
    "resolve-id": cmd_resolve_id,
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
