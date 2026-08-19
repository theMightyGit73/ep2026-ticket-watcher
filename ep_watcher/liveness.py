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


def _stored_until(state) -> float:
    """The persisted cooldown as epoch seconds, or 0.0 if there is none."""
    if not state:
        return 0.0
    raw = state.get("ntfy_cooldown_until")
    if not raw:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def due(now: float = None, state=None) -> bool:
    """Is another beacon worth sending yet?

    See config.LIVENESS_INTERVAL_MINUTES for why this is throttled at all, and
    LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES for why a refusal pushes it out much
    further than the ordinary interval.

    Takes the later of the process-local hold-off and the one written into
    state. The stored one is what survives a restart — and a restart is
    exactly when this matters, because launchd will restart the watcher for
    all sorts of reasons and each restart used to fire a request straight into
    an endpoint that was still refusing. That is how a rate limiter is held
    empty rather than allowed to refill.
    """
    now = time.time() if now is None else now
    return now >= max(_next_allowed, _stored_until(state))


def publish(note: str = "", force: bool = False, state=None) -> bool:
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
    if not (force or due(state=state)):
        return False

    # Stand down when the day is nearly spent. The beacon is the least
    # important thing this project publishes and was the thing that emptied
    # the quota; a ticket alert is the most important and must never find it
    # empty. Ceding the last messages is the same rule David set for tickets
    # — the important thing wins the scarce resource.
    from . import pushquota

    if not (force or pushquota.may_send(config.NTFY_ALERT_RESERVE)):
        # Standing down is also how the outage gets REPORTED. Once the
        # allowance is known to be gone the beacon stops attempting, so no
        # further 429 arrives — and the email announcing that push has
        # stopped would never fire, which is the exact silence this whole
        # change exists to remove. note_exhausted() is idempotent: it marks
        # the day and mails at most once.
        if pushquota.remaining() <= 0:
            pushquota.note_exhausted()
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
        from . import pushquota

        pushquota.note_exhausted()
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
        # Written down, so a restart does not throw the cooldown away and
        # immediately ask again. The caller saves the state it passed in.
        if state is not None:
            from datetime import datetime, timedelta, timezone

            state["ntfy_cooldown_until"] = (
                datetime.now(timezone.utc)
                + timedelta(minutes=config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES)
            ).isoformat()
        print(f"[{stamp()}] ntfy is rate limiting this client (429) — pausing "
              f"the liveness beacon for "
              f"{config.LIVENESS_RATE_LIMIT_COOLDOWN_MINUTES:.0f} min so the "
              f"quota can recover for real alerts")
        return False

    if resp.status_code == 200:
        from . import pushquota

        pushquota.note_sent()
        # Recovered: clear the persisted cooldown so it cannot outlive the
        # refusal that set it.
        if state is not None and state.get("ntfy_cooldown_until"):
            state["ntfy_cooldown_until"] = None
        return True
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
