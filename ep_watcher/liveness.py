"""A dead man's switch for the watcher on the Mac.

Everything else that keeps the watcher alive runs *on the Mac*: launchd
restarts it when it crashes, the watchdog kicks it when it hangs. All of it
shares one fatal assumption — that the Mac is on. If the laptop is shut, out
of battery, off the network, or simply closed by accident, every local
safeguard dies with it, and the only symptom is that the emails quietly stop.

Silence is exactly what this project refuses to treat as information.

So the Mac publishes a heartbeat to a separate ntfy topic on every poll, and
something entirely off the Mac — the hourly GitHub Actions job — reads that
topic and shouts if the heartbeat has gone stale. No new services, no
account, no cost: it reuses the push channel that is already there.

The heartbeat goes to its own topic (`<topic>-alive`) precisely so David does
not subscribe to it. It fires every few minutes; on his phone it would be
unusable noise, and he would mute the app that carries the ticket alert.
"""

import time
from typing import Optional

import requests

from . import config
from .state import stamp

#: Low priority and no tags: nothing about a heartbeat should ever buzz.
_PRIORITY = "1"


def topic() -> Optional[str]:
    if not config.NTFY_TOPIC:
        return None
    return f"{config.NTFY_TOPIC}-alive"


#: The earliest this process may publish again. Module-level rather than in
#: state.json because it is genuinely per-process: a restarted watcher SHOULD
#: announce itself immediately, since a restart is exactly the event the
#: switch exists to notice, and launchd already throttles restarts to one a
#: minute.
_next_allowed = 0.0


def due(now: float = None) -> bool:
    """Is another beacon worth sending yet?

    See config.LIVENESS_INTERVAL_MINUTES for why this is throttled at all, and
    LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES for why a refusal pushes it out much
    further than the ordinary interval.
    """
    return (time.time() if now is None else now) >= _next_allowed


def publish(note: str = "", force: bool = False) -> bool:
    """Say "still here". Never raises, never blocks a poll for long.

    A failed heartbeat must not fail the check that carries it — the watcher
    finding a ticket matters more than the watcher announcing it is well.

    Throttled, because the beacon and the ticket alert come out of the same
    ntfy quota and the beacon was eating it. `force` is for the commands that
    exist to prove the path works end to end, where skipping the send would
    make the check meaningless.
    """
    global _next_allowed

    t = topic()
    if not t:
        return False
    if not (force or due()):
        return False
    # Held off before the attempt, not after. A failing ntfy must not turn
    # into a retry on every single poll, which is the surest way to stay rate
    # limited — the next scheduled beacon is soon enough.
    _next_allowed = time.time() + config.LIVENESS_INTERVAL_MINUTES * 60
    try:
        resp = requests.post(
            f"https://ntfy.sh/{t}",
            data=(note or f"alive {int(time.time())}").encode("utf-8"),
            headers={"Title": "ep2026-alive", "Priority": _PRIORITY},
            timeout=8,
        )
    except requests.RequestException:
        return False

    if resp.status_code == 429:
        # Back a long way off, and say so once. Continuous requests hold a
        # rate limiter empty; on 2026-08-19 a beacon retrying every three
        # minutes kept ntfy refusing for 2.8 hours, which disabled the dead
        # man's switch and produced a "your Mac has gone quiet" email about a
        # watcher that was working perfectly.
        #
        # This is also the quota the ticket alert comes out of, so standing
        # back is not politeness — it is leaving room for the one message
        # this project exists to send.
        _next_allowed = (
            time.time() + config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES * 60)
        print(f"[{stamp()}] ntfy is rate limiting this client (429) — pausing "
              f"the liveness beacon for "
              f"{config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES:.0f} min so the "
              f"quota can recover for real alerts")
        return False

    return resp.status_code == 200


def age_seconds() -> Optional[float]:
    """How long since the Mac last said it was alive, in seconds.

    None means "cannot tell" — no topic configured, ntfy unreachable, or no
    heartbeat in the cache window. The caller must treat that differently
    from "definitely stale", because an ntfy outage is not a dead watcher and
    crying wolf about it would train David to ignore the one alert that says
    his watcher is genuinely down.
    """
    t = topic()
    if not t:
        return None
    try:
        resp = requests.get(
            f"https://ntfy.sh/{t}/json",
            params={"poll": "1", "since": "12h"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
    except requests.RequestException:
        return None

    latest = None
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json

            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") != "message":
            continue
        ts = msg.get("time")
        if isinstance(ts, (int, float)) and (latest is None or ts > latest):
            latest = ts

    if latest is None:
        return None
    return max(0.0, time.time() - latest)
