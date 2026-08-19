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
    """{"day", "count", "alerted_on"}, with the count reset each new day.

    `alerted_on` deliberately SURVIVES the day rolling over. It records the
    day an "push is down" email went out, and it is what allows the matching
    "push is back" email to be sent afterwards — recovery almost always
    happens on the following day, when the allowance resets, so a flag cleared
    at midnight could never report it.

    Anything unreadable counts as a fresh day. Being wrong here in the
    optimistic direction costs a few messages against the limit; being wrong
    in the pessimistic direction would suppress an alert, which is worse.
    """
    try:
        data = json.loads(_path().read_text())
        alerted = data.get("alerted_on")
        if data.get("day") == _today():
            return {"day": data["day"], "count": int(data.get("count") or 0),
                    "alerted_on": alerted}
        return {"day": _today(), "count": 0, "alerted_on": alerted}
    except (OSError, ValueError, TypeError):
        pass
    return {"day": _today(), "count": 0, "alerted_on": None}


def _write(data: dict) -> None:
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


def used() -> int:
    return _load()["count"]


def remaining() -> int:
    return max(0, config.NTFY_DAILY_LIMIT - used())


def note_sent(count: int = 1) -> None:
    """Record messages that ntfy actually accepted. Never raises.

    A send getting through while an outage is on record means the channel is
    back, so this is also where recovery is noticed. That is deliberate: no
    timer and no polling can tell you a rate limit has lifted, only a message
    that succeeds.
    """
    data = _load()
    data["count"] += count
    recovered_from = data.get("alerted_on")
    if recovered_from:
        data["alerted_on"] = None
    _write(data)
    if recovered_from:
        _email_recovered(recovered_from)


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
    first_today = data.get("alerted_on") != _today()
    if first_today:
        data["alerted_on"] = _today()
    _write(data)
    # Once a day, and by email, because email is the channel that still
    # works. Nothing said anything at all on 2026-08-19: the allowance went at
    # 16:55 and the first word David had was a false "your Mac has gone quiet"
    # from GitHub at 21:42, five hours in — hours during which a ticket alert
    # would have reached him by email only, minutes slower, on listings that
    # live minutes.
    if first_today:
        _email_exhausted()


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


# ── telling David, over the channel that still works ─────────────────────────
#
# These live here rather than in notify.py because the trigger lives here, and
# because the whole point is that they must fire from inside the failure. The
# import is deferred so that notify can go on importing this module without a
# cycle at import time.

def _email_exhausted() -> None:
    """Say that push has stopped, once per day, by email."""
    from . import notify

    notify._safe(
        "push-quota-email", notify._send_email,
        "EP2026: push notifications have stopped for today",
        f"Hi David,\n\n"
        f"The phone push channel is out of messages for today. Ticket alerts\n"
        f"will still reach you BY EMAIL — that is unaffected, and this message\n"
        f"is proof of it — but they will not buzz your phone until the\n"
        f"allowance resets, which happens daily.\n\n"
        f"Why: ntfy.sh allows an anonymous sender {config.NTFY_DAILY_LIMIT}\n"
        f"messages a day per connection, and that is now spent. The watcher\n"
        f"has stopped sending its own heartbeat so that whatever is left goes\n"
        f"to real alerts rather than to housekeeping.\n\n"
        f"WHAT THIS MEANS FOR YOU\n\n"
        f"  * A listing will still be emailed the moment it is found.\n"
        f"  * Email is a few minutes slower than push, and these listings\n"
        f"    live about that long — so keep an eye on the inbox tonight.\n"
        f"  * Securing is unaffected. If a ticket appears the watcher will\n"
        f"    still try to put it in a basket for you.\n"
        f"  * You may get 'your Mac watcher has gone quiet' emails from\n"
        f"    GitHub while this lasts. They are FALSE. That check works by\n"
        f"    watching for a heartbeat that travels over this same blocked\n"
        f"    channel, so it cannot see the Mac even though the Mac is fine.\n\n"
        f"THE PERMANENT FIX, which needs you\n\n"
        f"  A free ntfy.sh account raises the daily allowance well above the\n"
        f"  anonymous one. Sign up at https://ntfy.sh, then say so and the\n"
        f"  token goes in ~/.ep2026-watcher/env — about two minutes' work,\n"
        f"  and this stops happening.\n\n"
        f"Noticed at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
    )


def _email_recovered(since: str) -> None:
    """Say that push is working again. Only ever after having said it stopped."""
    from . import notify

    notify._safe(
        "push-recovered-email", notify._send_email,
        "EP2026: push notifications are working again",
        f"Hi David,\n\n"
        f"Phone push is back. A message has just gone through, so alerts will\n"
        f"buzz your phone again rather than arriving by email alone.\n\n"
        f"It stopped on {since}, when the daily allowance ran out.\n\n"
        f"Nothing was missed on the email side while it was down — that path\n"
        f"never stopped. Any 'your Mac watcher has gone quiet' emails you got\n"
        f"in between were caused by this and were false.\n\n"
        f"Noticed at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
    )
