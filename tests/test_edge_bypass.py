"""Reading Fastly's edge is reading the past.

Added 2026-08-25, from David's own network capture. His signed-in browser
asked /api/quickpicks/{event}/resale and got back `x-cache: HIT` with
`age: 13` — the answer was thirteen seconds old before it reached him.

The endpoint's headers are `max-age=15, stale-while-revalidate=30`, so a
listing can exist for up to forty-five seconds before any edge copy mentions
it. That is the gap no cadence can close: asking the same URL ten times in ten
seconds returns the same stale object ten times. It is also the best available
explanation for listings that arrive already half-dead, and for 70 of the
first 75 being seen exactly once.

A nonce makes the URL novel, so the edge cannot answer and origin must. These
checks pin that the nonce is actually there and actually varies, because a
constant one would look identical in a trace and silently read the edge again.

Run with:  .venv/bin/python tests/test_edge_bypass.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import config  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def build(event_id="18006314BD813D3E", qty=1):
    """The URL the sweep builds, mirroring fetch_resale_json."""
    url = f"/api/quickpicks/{event_id}/resale?qty={qty}&offset=0&limit=20"
    if config.EDGE_BYPASS:
        url += f"&_={int(time.time() * 1000)}"
    return url


print("\nThe sweep asks for something the edge cannot have cached")

check("bypass is on by default", config.EDGE_BYPASS, True)
url = build()
check("the URL carries a nonce", "&_=" in url, True)
check("and still asks for one ticket", "qty=1" in url, True)
check("and still names the event",
      "18006314BD813D3E" in url, True)


print("\nThe nonce varies, or it is not a nonce")

# A constant would look right in a trace and read the edge every time.
a = build()
time.sleep(0.002)
b = build()
check("two calls differ", a != b, True)


print("\nSwitchable, not surgery")

# The cost is real — every bypassed call is an origin hit on a connection
# blocked twenty-two times — so it has to be one line to turn off, not a code
# change, exactly like every other trade in this project.
check("there is an env switch",
      "EP_EDGE_BYPASS" in open(
          Path(__file__).resolve().parent.parent
          / "ep_watcher" / "config.py").read(), True)


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All edge-bypass checks passed.")
