"""The buying browser must be able to start while the watcher's is running.

On 2026-08-19 three real listings appeared — two Early Entry passes at €46.50
and a Weekend Camping at €366.39. All three produced a correct availability
alert, and all three securing attempts died before opening anything:

    Error: It looks like you are using Playwright Sync API inside the asyncio
    loop. Please use the Async API instead.

Playwright's sync API refuses to start a second instance in a thread that
already has an asyncio loop, and the watcher's own browser keeps one running
for the life of the process. So the feature could never have worked, on any
listing, ever — and no offline test caught it, because the fault only exists
when a second Playwright starts inside a live one. The whole test suite was
green while the thing it was testing could not run.

That is the lesson these checks are for: the failure was architectural and
only reproducible against the real library. So this file uses the real
Playwright, and is skipped rather than failed where it is not installed.

Run with:  .venv/bin/python tests/test_secure_threading.py
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("\n  [ -- ]  Playwright not installed — skipping (API-only deployment)\n")
    sys.exit(0)

from ep_watcher import buyer  # noqa: E402


print("\nA second sync Playwright in the SAME thread is what broke it")
# Reproducing the bug, so the fix is measured against the real failure rather
# than against a description of it. Nothing here opens a browser: starting the
# driver is enough to trigger it.
outer = sync_playwright().start()
try:
    same_thread_error = ""
    try:
        inner = sync_playwright().start()
        inner.stop()
    except Exception as exc:
        same_thread_error = f"{type(exc).__name__}: {exc}"

    check_true("starting a second Playwright in the same thread fails",
               same_thread_error != "")
    check_true("and fails with the message David was emailed three times",
               "asyncio" in same_thread_error.lower()
               or "sync api" in same_thread_error.lower())

    print("\nA fresh thread has no event loop, so it starts cleanly")
    # The fix. A thread of its own is not an optimisation here — it is the
    # only arrangement in which the buying browser can exist at all.
    box = {}

    def start_in_thread():
        try:
            pw = sync_playwright().start()
            pw.stop()
            box["ok"] = True
        except Exception as exc:
            box["ok"] = False
            box["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=start_in_thread)
    worker.start()
    worker.join(timeout=60)
    check("a second Playwright starts in its own thread", box.get("ok"), True)
    if not box.get("ok"):
        print(f"        error was: {box.get('error')}")
finally:
    outer.stop()


print("\nsecure_in_thread never raises into the poll loop")
# Whatever happens to the browser, the watcher must keep polling. The
# availability alert has already gone out by this point; securing is the
# optimistic extra and is not allowed to cost the thing that works.


class Boom:
    def __init__(self, *a, **kw):
        raise RuntimeError("no browser here")


real_session = buyer.BuySession
try:
    buyer.BuySession = Boom
    hold = buyer.secure_in_thread(object(), object())
    check("a browser that cannot start returns a result", hold.secured, False)
    check_true("with the reason carried back out of the thread",
               "no browser here" in (hold.reason or ""))
finally:
    buyer.BuySession = real_session


print("\nA hung securing attempt is abandoned, not waited on")
# A wedged Chrome must not stop the watcher looking for the next listing.


class Hangs:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        threading.Event().wait(30)      # never set
        return self

    def close(self):
        pass


try:
    buyer.BuySession = Hangs
    import time

    began = time.monotonic()
    hold = buyer.secure_in_thread(object(), object(), timeout_s=2)
    took = time.monotonic() - began
    check_true("it gives up close to its deadline", took < 6)
    check("and reports no hold", hold.secured, False)
    check_true("saying it was abandoned", "abandoned" in (hold.reason or ""))
finally:
    buyer.BuySession = real_session


print("\nThe engine hands securing to the thread, never to the poll loop")
engine_source = (Path(__file__).resolve().parent.parent
                 / "ep_watcher" / "engine.py").read_text()
check_true("the engine calls the threaded entry point",
           "secure_in_thread" in engine_source)
check("and no longer starts a BuySession inline",
      "buyer.BuySession()" in engine_source, False)

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
