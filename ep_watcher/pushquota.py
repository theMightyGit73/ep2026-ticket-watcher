"""How many ntfy messages this machine has sent today, and whether to send more.

ntfy.sh allows an anonymous publisher a fixed number of messages a day per IP.
On 2026-08-19 the watcher spent them without noticing: the liveness beacon
published on every handled reading, 95 of them between 10:08 and 16:55 UTC —
a rate of 336 a day against a limit of 250 — and at 16:55 the server began
answering

    {"code":42908,"http":429,"error":"limit reached: daily message quota
     reached; increase your limits with a paid plan, see https://ntfy.sh"}

Nothing on this machine knew. The first symptom arrived five hours later as an
email from GitHub saying the Mac had gone quiet, about a watcher that was
running perfectly. In between, the push channel was dead — and that is the
channel a ticket alert travels on. A listing appearing in those five hours
would have reached David by email only, minutes slower, on a product whose
listings live about as long as the delay.

This module exists so that cannot happen silently again. It counts, it
reports, and it makes the beacon yield: when the day's remaining messages fall
below a reserve, the heartbeat stops and the quota is kept for alerts. That is
the same rule David set for tickets — the important thing wins the scarce
resource — applied to the other scarce resource in the system.

Its own file rather than a key in state.json, deliberately. state.json is read
and written wholesale by the poll loop, so a counter updated from inside a
notification would be clobbered by the save at the end of the cycle it was
written during.
"""

import json
from datetime import datetime, timezone

from . import config


def _path():
    return config.STATE_FILE.parent / "push-quota.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    """{"day": "YYYY-MM-DD", "count": N}, reset when the day rolls over.

    Anything unreadable counts as a fresh day. Being wrong here in the
    optimistic direction costs a few messages against the limit; being wrong
    in the pessimistic direction would suppress an alert, which is worse.
    """
    try:
        data = json.loads(_path().read_text())
        if data.get("day") == _today():
            return {"day": data["day"], "count": int(data.get("count") or 0)}
    except (OSError, ValueError, TypeError):
        pass
    return {"day": _today(), "count": 0}


def used() -> int:
    return _load()["count"]


def remaining() -> int:
    return max(0, config.NTFY_DAILY_LIMIT - used())


def note_sent(count: int = 1) -> None:
    """Record messages that ntfy actually accepted. Never raises."""
    data = _load()
    data["count"] += count
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        # Losing the count costs accounting, not delivery. Never let it break
        # the send it is counting.
        pass


def note_exhausted() -> None:
    """The server says the day's allowance is gone. Believe it over our count.

    Our tally only knows what this process has sent since the counter existed.
    On the day it was introduced ntfy was already refusing — code 42908,
    "daily message quota reached" — while the local count stood at zero,
    so doctor cheerfully reported 250 messages remaining beside a line saying
    the quota was spent. The server is the authority on its own limit, and a
    check that contradicts the thing it is checking teaches people to ignore
    it.
    """
    data = _load()
    data["count"] = max(data["count"], config.NTFY_DAILY_LIMIT)
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass


def may_send(reserve: int = 0) -> bool:
    """Is there room for another message, keeping `reserve` back?

    `reserve` is how many are held for messages that matter. The beacon passes
    the reserve and therefore stops early; a ticket alert passes nothing and
    may spend down to the last message.
    """
    return remaining() > reserve


def summary() -> str:
    """One line for doctor."""
    spent, left = used(), remaining()
    return (f"{spent} of {config.NTFY_DAILY_LIMIT} messages used today, "
            f"{left} left")
