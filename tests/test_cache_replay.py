"""A retry that never leaves the machine is not a retry.

Added 2026-08-24, after the logs showed that most of what the chase called
evidence was Chrome talking to itself.

Ticketmaster answers the offer URL with a 302 to /error/q404, and that
redirect is cacheable. Chrome cached it, so the second and later visits to a
listing were served from disk — the request never went out, and the refusal
being recorded was a copy of the first one. The traces measure it exactly:
the first attempt on a listing takes 120-330ms, and the retries came back in
1-2ms, which is not a possible round trip to Dublin. On listing l0vmtvwkd2,
ten of fourteen retries were replays; the four real ones produced four
distinct Ticketmaster error ids and the ten produced none.

That is worse than wasted time. A retry that cannot observe a change makes the
chase logically incapable of succeeding, and every identical "still refused"
it logged was then read as proof the listing was still held — proof the
browser manufactured by not asking. A fortnight of work was aimed at that
reading.

So two properties are pinned here: the buyer asks the network rather than the
cache, and when a reply arrives too fast to be real it is recorded as a replay
instead of being counted as a refusal.

Run with:  .venv/bin/python tests/test_cache_replay.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


class FakeResult:
    def __init__(self):
        self.notes = []
        self.cache_replays = 0

    def note(self, text):
        self.notes.append(text)


print("\nThe first attempt sends Ticketmaster's own URL, untouched")

# The attempt most likely to succeed must carry no invention of ours. It also
# needs no help: nothing is cached for a listing being visited for the first
# time.
BASE = "https://secure.ticketmaster.ie/18006314BD813D3E/l0vmtvwkd2?qty=1"
check("attempt 1 is unchanged", buyer.uncached_offer_url(BASE, 1), BASE)


print("\nA retry is made distinct, so Chrome cannot replay it")

retry = buyer.uncached_offer_url(BASE, 2)
check("attempt 2 differs from attempt 1", retry != BASE, True)
check("and still points at the same listing", "l0vmtvwkd2" in retry, True)
check("and still asks for one ticket", "qty=1" in retry, True)

# Two retries must not collide, or the second replays the first.
import time as _t
a = buyer.uncached_offer_url(BASE, 2)
_t.sleep(0.002)
b = buyer.uncached_offer_url(BASE, 3)
check("two retries get different URLs", a != b, True)


print("\nThe scope is the request, never the page")

# The first version of this fix used page.set_extra_http_headers, whose
# docstring claimed it "affects nothing else". It is sticky for the life of
# the page, so it actually put no-cache on every later request the buying
# browser made — the parked event page and every poll of the rate-limited
# resale endpoint. The second attempt of the 10:10 chase on 2026-08-25 never
# returned; the worker sat in it for 390s and the next listing was refused
# because the browser was "busy".
#
# So nothing here may touch page-wide state. A pure function over a string
# cannot: there is no browser round trip to hang in and nothing to leave
# switched on.
check("no page-level header helper survives",
      hasattr(buyer, "_disable_offer_cache"), False)
check("the helper is a pure string function",
      callable(getattr(buyer, "uncached_offer_url", None)), True)


print("\nA reply too fast to be real is recorded as a replay")

# 1.5ms. Every cache replay measured on 2026-08-24 fell in this range, and
# nothing that reached Dublin was ever under 120ms.
result = FakeResult()
check("1.5ms is called a replay", buyer._note_if_cached(result, 0.0015), True)
check("and it is counted", result.cache_replays, 1)
check("and the note says it proves nothing about the listing",
      any("browser cache" in n for n in result.notes), True)

# 250ms — an ordinary refusal from Ticketmaster. Must NOT be dismissed as a
# replay, or a real refusal would stop counting and the alarm would never trip.
result = FakeResult()
check("250ms is a real answer", buyer._note_if_cached(result, 0.25), False)
check("and is not counted as a replay", result.cache_replays, 0)
check("and says nothing", result.notes, [])


print("\nThe threshold sits in the gap between cache and network")

# The two populations are three orders of magnitude apart, so the exact value
# matters less than that it lies between them. If someone tunes this, it must
# stay in the gap — above every replay, below the fastest real round trip.
check("above the replays measured at 1-2ms",
      config.CACHE_REPLAY_SECONDS > 0.002, True)
check("below the fastest real answer measured at 120ms",
      config.CACHE_REPLAY_SECONDS < 0.12, True)


print("\nThe builder itself still produces Ticketmaster's exact URL")

class Ev:
    url = ("https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping"
           "-co-laois-28-08-2026/event/18006314BD813D3E")

url = buyer.offer_url(Ev(), "l0vmtvwkd2")
check("the URL is exactly event/listing/qty",
      url,
      "https://secure.ticketmaster.ie/18006314BD813D3E/l0vmtvwkd2?qty=1")
check("no cache-busting parameter was smuggled in",
      "_=" in url or "nonce" in url or "cb=" in url, False)

# Built twice, identical. A URL that changed between attempts would defeat the
# cache by accident and hide whether the header is doing its job.
check("and it is stable across calls", buyer.offer_url(Ev(), "l0vmtvwkd2"), url)


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All cache-replay checks passed.")
