"""Nothing may restart the watcher while it is holding a ticket.

This is the most expensive failure in the codebase, and it was live until
2026-08-19. Every other bug here costs a ticket that was never in hand; this
one destroys a ticket already caught, silently, using the machinery built to
keep the watch alive.

The mechanism: a Ticketmaster basket lives in the browser session that created
it, and that browser is a child of the watcher process. The watchdog restarts
a watcher whose poll clock has stopped advancing — which is correct in every
case but one, because a watcher pausing for a checkout looks exactly like a
watcher wedged on a hung Chrome. On the primary-stock path the watcher printed
"Reserve accepted — pausing the loop so you can check out" and then slept
forever without writing anything down. Fifteen minutes later
`launchctl kickstart -k` would have killed it, and the ticket with it.

So a live hold is now written into state, the watchdog reads it, and the
protection is bounded — because a hold nobody completes must not silence the
watch for the rest of the fortnight.

Run with:  .venv/bin/python tests/test_hold_not_restarted.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def run_watchdog(state_dict, stale_minutes=45):
    """Run the real watchdog.sh against a fixture state, in dry-run."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(state_dict, f)
        path = f.name
    log = path + ".log"
    try:
        env = dict(os.environ)
        env.update({
            "EP_STATE_FILE": path,
            "EP_WATCHDOG_DRY_RUN": "1",
            "EP_STALE_MINUTES": str(stale_minutes),
            # Never the real log. These runs would otherwise write "restarting
            # the watcher" into the file you read when something has actually
            # gone wrong.
            "EP_WATCHDOG_LOG": log,
        })
        result = subprocess.run(
            ["bash", str(REPO / "watchdog.sh")],
            capture_output=True, text=True, env=env, timeout=60,
        )
        return "WOULD_RESTART" in result.stdout
    finally:
        os.unlink(path)
        if os.path.exists(log):
            os.unlink(log)


WEDGED = (st.utc_now() - timedelta(minutes=90)).isoformat()
SOON = (st.utc_now() + timedelta(minutes=12)).isoformat()
LAPSED = (st.utc_now() - timedelta(minutes=5)).isoformat()


print("\nThe bug, stated as a test: a stopped clock during a checkout")
# Without the marker this is indistinguishable from a hang, and the watchdog
# does the one thing that loses the ticket.
check("a watcher with a stopped clock and no hold IS restarted — correctly",
      run_watchdog({"last_check_at": WEDGED}), True)
check("but the same clock WITH a live hold is left alone",
      run_watchdog({"last_check_at": WEDGED, "hold_until": SOON}), False)

print("\nThe protection is bounded, so it cannot silence the watch forever")
check("a hold whose window has passed no longer protects anything",
      run_watchdog({"last_check_at": WEDGED, "hold_until": LAPSED}), True)
check("and neither does a null marker",
      run_watchdog({"last_check_at": WEDGED, "hold_until": None}), True)
check("nor a corrupt one — it must fail towards restarting, not towards silence",
      run_watchdog({"last_check_at": WEDGED, "hold_until": "not a timestamp"}), True)

print("\nThe same protection covers an attempt still in progress")
# The trap arrived at from the other side, on 2026-08-24. The buyer now chases
# a listing Ticketmaster's error page calls active for up to twelve minutes,
# and submit() blocks the poll loop throughout — so the clock goes twelve
# minutes stale against a GRACE of fifteen, and what a restart kills is the
# buying browser mid-chase. Two minutes is not a margin.
check("a watcher mid-attempt is left alone",
      run_watchdog({"last_check_at": WEDGED, "securing_until": SOON}), False)
check("an attempt whose budget has passed protects nothing",
      run_watchdog({"last_check_at": WEDGED, "securing_until": LAPSED}), True)
check("nor does a null marker",
      run_watchdog({"last_check_at": WEDGED, "securing_until": None}), True)
check("nor a corrupt one — fail towards restarting, never towards silence",
      run_watchdog({"last_check_at": WEDGED,
                    "securing_until": "not a timestamp"}), True)

print("\nThe marker is bounded by the buyer's own budget")
fresh = dict(st.DEFAULTS) if hasattr(st, "DEFAULTS") else {}
st.note_securing(fresh)
left = st.securing_remaining(fresh)
check_true = lambda label, got: check(label, bool(got), True)
check_true("it outlasts the longest legitimate chase",
           left > config.secure_budget_seconds() - 1)
check_true("but not by much — a dead process must not mute the watchdog",
           left <= config.secure_budget_seconds() + 120)
st.clear_securing(fresh)
check("and clearing it hands the watchdog back its job at once",
      st.securing_remaining(fresh), 0.0)

print("\nIt does not interfere with the checks that were already there")
healthy = st.utc_now().isoformat()
check("a healthy watcher is still left alone",
      run_watchdog({"last_check_at": healthy}), False)
check("a 403 backoff still protects a resting watcher",
      run_watchdog({"last_check_at": WEDGED, "backoff_until": SOON}), False)

print("\nThe state helpers themselves")
state = dict(st._defaults())
check("a fresh state is not holding anything", st.hold_remaining(state), 0.0)
st.note_hold(state, 20)
check("a 20-minute hold reads back as 20 minutes",
      round(st.hold_remaining(state) / 60.0), 20)
st.clear_hold(state)
check("and clears cleanly", st.hold_remaining(state), 0.0)
state["hold_until"] = "not a timestamp"
check("garbage reads as no hold rather than raising", st.hold_remaining(state), 0.0)

print("\nThe window is the hold plus a margin, and prefers a measured countdown")
check("with nothing measured it is the configured estimate plus the margin",
      config.hold_window_minutes(),
      config.HOLD_MINUTES_HINT + config.HOLD_PAUSE_EXTRA_MINUTES)
check("a countdown read off the page is used instead",
      config.hold_window_minutes(11), 11 + config.HOLD_PAUSE_EXTRA_MINUTES)
check_true("and the margin is real, not zero — a checkout takes longer than the hold",
           config.HOLD_PAUSE_EXTRA_MINUTES > 0)

print("\nA successful hold writes the marker; a failed one must not")
# The asymmetry matters. Marking on a failure would tell the watchdog to leave
# a genuinely broken watcher alone, for a ticket that was never held.
import smtplib  # noqa: E402
from ep_watcher import buyer, engine, notify  # noqa: E402
from ep_watcher.model import AVAILABLE, UNAVAILABLE, Listing, Reading  # noqa: E402


class FakeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, msg): pass


smtplib.SMTP_SSL = FakeSMTP
notify.requests = type("_NoPush", (), {"post": staticmethod(lambda *a, **kw: None)})()
engine.network = type("_Net", (), {
    "public_ip": staticmethod(lambda *a, **kw: "10.0.0.1"),
    "fingerprint": staticmethod(lambda *a, **kw: {"key": "aa:bb", "ip": "10.0.0.1"}),
})()
config.GMAIL_ADDRESS = "davidcoyne73@gmail.com"
config.GMAIL_APP_PASSWORD = "x"
config.NTFY_TOPIC = None


def a_find():
    reading = Reading(source="browser", primary=UNAVAILABLE, resale=AVAILABLE)
    reading.event_slug = config.EVENTS[0].slug
    reading.event_name = config.EVENTS[0].name
    reading.event_url = config.EVENTS[0].url
    reading.listings.append(
        Listing("Verified Resale — Section STNDN1", "€366.39", "resale",
                listing_id="l27t4h2d", section="STNDN1"))
    return reading


def handle_with(hold):
    fresh = dict(st._defaults())
    was_flag, was_secure = config.SECURE_ON_FIND, buyer.secure_in_thread
    try:
        config.SECURE_ON_FIND = True
        buyer.secure_in_thread = lambda *a, **kw: hold
        engine.handle(a_find(), fresh)
    finally:
        config.SECURE_ON_FIND, buyer.secure_in_thread = was_flag, was_secure
    return fresh


held = handle_with(buyer.HoldResult(secured=True, minutes_hint=11,
                                    minutes_measured=True))
check_true("a held ticket is written into state", st.hold_remaining(held) > 0)
check("for the measured countdown plus the margin",
      round(st.hold_remaining(held) / 60.0),
      round(config.hold_window_minutes(11)))
check_true("and the watchdog would leave that watcher alone",
           not run_watchdog({"last_check_at": WEDGED,
                             "hold_until": held["hold_until"]}))

missed = handle_with(buyer.HoldResult(secured=False, reason="it was gone"))
check("a failed attempt writes no hold at all", st.hold_remaining(missed), 0.0)
check_true("so a broken watcher is still restartable",
           run_watchdog({"last_check_at": WEDGED,
                         "hold_until": missed.get("hold_until")}))


print("\nThe checkout pause itself: bounded, marked, and self-clearing")
# The pause used to be `while True: time.sleep(30)` — unbounded and silent.
# "Ctrl-C when you're done" is fine at a terminal and meaningless under
# launchd, where nobody is at a keyboard, so a hold David never noticed would
# have stopped the watch until somebody thought to look.
import tempfile as _tempfile  # noqa: E402
import time as _time  # noqa: E402

from ep_watcher import __main__ as _cli  # noqa: E402

_was_state = config.STATE_FILE
_was_hint = config.HOLD_MINUTES_HINT
_was_extra = config.HOLD_PAUSE_EXTRA_MINUTES
_seen_while_paused = []
_real_sleep = _time.sleep
with _tempfile.TemporaryDirectory() as _tmp:
    try:
        config.STATE_FILE = Path(_tmp) / "state.json"
        # A three-second window, so the real loop runs for real rather than
        # being stubbed into something that proves nothing.
        config.HOLD_MINUTES_HINT = 0.02
        config.HOLD_PAUSE_EXTRA_MINUTES = 0.03

        def _watch_sleep(seconds):
            # Read the marker from the same file the watchdog would read,
            # while the pause is actually in progress.
            _seen_while_paused.append(st.hold_remaining(st.load()))
            _real_sleep(min(seconds, 0.4))

        _cli.time.sleep = _watch_sleep
        started = _time.monotonic()
        _cli._pause_for_checkout()
        took = _time.monotonic() - started
    finally:
        _cli.time.sleep = _real_sleep
        config.STATE_FILE = _was_state
        config.HOLD_MINUTES_HINT = _was_hint
        config.HOLD_PAUSE_EXTRA_MINUTES = _was_extra
        final = st.load() if _was_state else {}

check_true("the pause ended on its own rather than blocking forever", took < 30)
check_true("and it actually paused rather than falling straight through",
           len(_seen_while_paused) >= 1)
check_true("the hold marker was live the whole time it was paused",
           all(remaining > 0 for remaining in _seen_while_paused))
check_true("and it counted down rather than sitting at the full window",
           len(_seen_while_paused) < 2
           or _seen_while_paused[-1] <= _seen_while_paused[0])


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
