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


class FakePage:
    """Records the headers it was handed, and can refuse to take them."""

    def __init__(self, explode=False):
        self.headers = None
        self.explode = explode

    def set_extra_http_headers(self, headers):
        if self.explode:
            raise RuntimeError("browser said no")
        self.headers = dict(headers)


class FakeResult:
    def __init__(self):
        self.notes = []
        self.cache_replays = 0

    def note(self, text):
        self.notes.append(text)


print("\nThe navigation asks Ticketmaster, not Chrome")

page = FakePage()
buyer._disable_offer_cache(page)
check("a no-cache header is set before the offer loads",
      (page.headers or {}).get("Cache-Control"), "no-cache")
check("and the HTTP/1.0 spelling too, for anything that only reads that",
      (page.headers or {}).get("Pragma"), "no-cache")


print("\nA browser that refuses the header still gets its navigation")

# The old behaviour is a cached retry, which is bad. A crash here would be
# worse: it would lose the attempt entirely, on the one path that reaches a
# checkout. Degrade, say so, carry on.
exploding = FakePage(explode=True)
result = FakeResult()
check("no exception escapes", buyer._disable_offer_cache(exploding, result), False)
check("and the attempt is told the cache may answer",
      any("cache" in n for n in result.notes), True)


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


print("\nThe offer URL itself is left alone")

# Deliberate: a nonce would defeat the cache too, but it changes the request
# Ticketmaster sees on the one path already being refused for reasons not yet
# understood. Adding an unknown parameter there could introduce a second cause
# and make the first unreadable. The fix changes who answers, not what is
# asked.
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
