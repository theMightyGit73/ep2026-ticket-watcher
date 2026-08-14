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

#: Low priority and no tags: nothing about a heartbeat should ever buzz.
_PRIORITY = "1"


def topic() -> Optional[str]:
    if not config.NTFY_TOPIC:
        return None
    return f"{config.NTFY_TOPIC}-alive"


def publish(note: str = "") -> bool:
    """Say "still here". Never raises, never blocks a poll for long.

    A failed heartbeat must not fail the check that carries it — the watcher
    finding a ticket matters more than the watcher announcing it is well.
    """
    t = topic()
    if not t:
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{t}",
            data=(note or f"alive {int(time.time())}").encode("utf-8"),
            headers={"Title": "ep2026-alive", "Priority": _PRIORITY},
            timeout=8,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


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
