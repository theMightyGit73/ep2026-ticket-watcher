"""The buying browser kept warm, and every way that must not go wrong.

Measured on 2026-08-20: once the detection lag was fixed by the resale sweep,
the entire remaining gap was the ~60 seconds between seeing a listing and
clicking its row — and almost all of that was a cold Chrome launch and the
event page's 401-then-reload dance. Work that does not depend on the listing,
done while the clock was running, on listings consumed in well under a minute.

So the browser is opened at startup and parked. What is tested here is mostly
the things that would make that a bad idea:

  * a warm browser must never be reused while a basket is live in it — that
    is the most expensive mistake available to this codebase
  * a worker that cannot start must be invisible, not fatal: the cold path
    has always worked and must keep working
  * a definite refusal must come back as an answer, NOT as a fallback, or a
    second browser gets opened on the same profile while a ticket is held
  * every page interaction belongs to the thread that owns the session,
    because Playwright's sync objects are not shareable — the fault that made
    securing impossible on 2026-08-19

No browser and no network: the session is a stand-in and secure() is replaced.

Run with:  .venv/bin/python tests/test_warm_buyer.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import buyer, config  # noqa: E402
from ep_watcher.buyer import BuyerWorker, HoldResult  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


EVENT = next(e for e in config.EVENTS if e.secure)
OTHER = next(e for e in config.EVENTS if e.slug != EVENT.slug)
LISTING = object()


class FakePage:
    def __init__(self, log):
        self.log = log

    def goto(self, url, wait_until=None):
        self.log.append(("goto", url, threading.current_thread().name))


class FakeSession:
    def __init__(self, log):
        self.page = FakePage(log)
        self.closed = False

    def close(self):
        self.closed = True


def worker_with(secure_impl, start_impl=None, home=None):
    """A BuyerWorker whose browser and secure() are stand-ins."""
    log = []
    sessions = []

    def fake_start(self):
        if start_impl:
            start_impl()
        s = FakeSession(log)
        sessions.append(s)
        return s

    was_start, was_secure = buyer.BuySession.start, buyer.secure
    buyer.BuySession.start = fake_start
    buyer.secure = secure_impl
    w = BuyerWorker(home=home or EVENT)
    w._restore = lambda: (setattr(buyer.BuySession, "start", was_start),
                          setattr(buyer, "secure", was_secure))
    w.log, w.sessions = log, sessions
    return w


def held(*_a, **_k):
    r = _a[3] if len(_a) > 3 else HoldResult()
    r.secured = True
    r.minutes_hint = 11
    return r


def lost(*_a, **_k):
    r = _a[3] if len(_a) > 3 else HoldResult()
    r.secured = False
    r.reason = "the listing had genuinely sold"
    return r


print("\nIt comes up warm, and parks on the page it will most likely need")
w = worker_with(lost)
w.start()
check_true("it becomes ready", w.wait_until_ready(timeout=5))
check("and reports itself idle", w.state, "idle")
check_true("available for work", w.available)
gotos = [c for c in w.log if c[0] == "goto"]
check_true("it navigated at startup, before any listing existed", len(gotos) >= 1)
check_true("to the page it is parked on", EVENT.url in gotos[0][1])
check("every page call came from the worker's own thread",
      {c[2] for c in w.log}, {"ep-buyer-warm"})
w.shutdown(); w._restore()


print("\nA find on the parked page skips navigation entirely — the whole point")
w = worker_with(lost)
w.start(); w.wait_until_ready(timeout=5)
before = len([c for c in w.log if c[0] == "goto"])
r = w.submit(EVENT, LISTING, HoldResult(), timeout_s=5)
check_true("an answer came back", r is not None)
check_true("and it noted the browser was already warm",
           any("already warm" in n for n in r.notes))
check_true("a 'warm' step was timed rather than a navigate",
           "warm" in r.timings and "navigate" not in r.timings)
w.shutdown(); w._restore()


print("\nA find on a DIFFERENT page navigates, and says so")
w = worker_with(lost)
w.start(); w.wait_until_ready(timeout=5)
r = w.submit(OTHER, LISTING, HoldResult(), timeout_s=5)
check_true("it navigated for the other page", "navigate" in r.timings)
w.shutdown(); w._restore()


print("\nWhile a basket is live, nothing may touch the browser")
# The most expensive mistake in the codebase. A warm browser holding a ticket
# must refuse work rather than reuse itself, and must not be re-parked or
# reloaded — navigating away is exactly what drops the basket.
w = worker_with(held)
w.start(); w.wait_until_ready(timeout=5)
r = w.submit(EVENT, LISTING, HoldResult(), timeout_s=5)
check("the hold succeeded", r.secured, True)
check("and the worker is now holding", w.state, "holding")
check("so it is not available", w.available, False)
check_true("and holding reads true", w.holding)

parks_while_holding = len([c for c in w.log if c[0] == "goto"])
second = w.submit(EVENT, LISTING, HoldResult(), timeout_s=5)
check_true("a second attempt is refused with a reason, not silently",
           second is not None and not second.secured)
check_true("and the reason names the live hold",
           "already open holding" in (second.reason or ""))
check("refusing did not navigate, which would drop the basket",
      len([c for c in w.log if c[0] == "goto"]), parks_while_holding)
check("and the worker is still holding", w.state, "holding")

print("\n...until it is released")
w.release()
deadline = time.time() + 5
while w.state == "holding" and time.time() < deadline:
    time.sleep(0.05)
check("release frees the browser", w.state, "idle")
check_true("and it navigated away, dropping whatever was left",
           len([c for c in w.log if c[0] == "goto"]) > parks_while_holding)
w.shutdown(); w._restore()


print("\nPreemption is allowed to drop a hold, but only when granted")
w = worker_with(held)
w.start(); w.wait_until_ready(timeout=5)
w.submit(OTHER, LISTING, HoldResult(), timeout_s=5)
check("a hold is live", w.state, "holding")
r = w.submit(EVENT, LISTING, HoldResult(), timeout_s=5, may_preempt=True)
check_true("the more important ticket is attempted", r is not None)
check_true("the earlier hold was dropped on purpose", r.preempted)
check_true("and it says so in the notes",
           any("dropping the live hold" in n for n in r.notes))
w.shutdown(); w._restore()


print("\nA browser that will not start is invisible, not fatal")
# The cold path has always worked. A warm browser is a speed-up and must never
# become a dependency.
def explode():
    raise RuntimeError("no Chrome on this machine")


w = worker_with(lost, start_impl=explode)
w.start()
check("it does not become available", w.wait_until_ready(timeout=5), False)
check("and marks itself dead", w.state, "dead")
check("submit hands the caller back to the cold path",
      w.submit(EVENT, LISTING, HoldResult(), timeout_s=2), None)
w._restore()


print("\nsecure_in_thread falls back when the worker declines, not when it answers")
# The distinction that stops a second browser opening on the same profile
# while a basket is live.
class Declines:
    available = True
    holding = False
    state = "idle"

    def submit(self, *a, **kw):
        return None


class Answers:
    available = True
    holding = False
    state = "idle"

    def submit(self, event, listing, result, timeout_s, may_preempt=False):
        result.reason = "a definite refusal"
        return result


cold_calls = []
was_session = buyer.BuySession


class NoBrowser:
    def start(self):
        cold_calls.append(1)
        raise RuntimeError("cold start reached")


buyer.BuySession = NoBrowser
try:
    buyer.secure_in_thread(EVENT, LISTING, timeout_s=3, worker=Declines())
    check_true("a None from the worker reaches the cold path", cold_calls)
    cold_calls.clear()
    out = buyer.secure_in_thread(EVENT, LISTING, timeout_s=3, worker=Answers())
    check("a real answer is returned as-is", out.reason, "a definite refusal")
    check("and the cold path is NOT reached", cold_calls, [])
finally:
    buyer.BuySession = was_session


print("\nA worker that hangs is abandoned, not waited on")
def slow(*_a, **_k):
    time.sleep(30)
    return HoldResult(secured=True)


w = worker_with(slow)
w.start(); w.wait_until_ready(timeout=5)
began = time.time()
r = w.submit(EVENT, LISTING, HoldResult(), timeout_s=1.0)
took = time.time() - began
check_true(f"it gave up promptly ({took:.1f}s)", took < 5)
check_true("and said why", "abandoned" in (r.reason or ""))
w._restore()

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
