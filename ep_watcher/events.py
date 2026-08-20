"""A machine-readable record of what the watcher did, beside the human one.

`watcher.log` is written for a person reading it at three in the morning, and
it is good at that. It is bad at every other question. Answering "how long was
the gap before each find?", "how many polls went resale-blind on this
connection?" or "did that block come before or after the sweep started?"
means writing a throwaway parser against prose — which is exactly what was
done on 2026-08-20, three times, badly. One of those parsers reported almost
every poll as a find, because `"AVAILABLE" in "UNAVAILABLE"` is true. The
answer it produced was wrong and looked entirely plausible.

That is the argument for this file. Prose is for humans; questions deserve
data. One JSON object per line, appended, never rewritten:

    {"ts": "2026-08-20T11:48:01+00:00", "kind": "find", "event": "weekend-camping",
     "via": "search", "price": "€366.39", "listing_ids": ["l5mm1z9t1s"]}

Deliberately NOT a replacement for the log or for state.json. The log stays
readable and state.json stays authoritative; this is a third thing, and the
only one of the three that can be queried. It is also the only one that can be
deleted without consequence, which is why it is written cheaply — appended and
flushed, never fsynced. Losing the tail of a diagnostic file costs a question;
paying for durability on every poll would cost the poll.

Nothing here may raise. A record of the work must never be able to stop the
work.
"""

import json
import os
from datetime import datetime, timezone

from . import config

#: Values longer than this are truncated. A rogue caller passing a whole page
#: of HTML would otherwise turn a diagnostic file into the thing that fills
#: the disk — on the machine that has to stay responsive enough to buy a
#: ticket.
MAX_VALUE_CHARS = 2_000


def path():
    """Where the event log lives. Beside watcher.log, and rotated with it."""
    return config.LOG_DIR / "events.jsonl"


def _clean(value):
    """Make one field safe to serialise, without losing what it meant."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + "…"
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in list(value)[:50]]
    # Anything else — a Reading, a Path, an exception — becomes its string.
    # Better a readable approximation than a line that will not serialise.
    return _clean(str(value))


def emit(kind: str, **fields) -> bool:
    """Append one event. Returns True if it was written.

    Never raises, and never blocks on anything slower than a line append. The
    return value exists for the tests rather than for callers: no caller
    should change what it does because a diagnostic write failed.
    """
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": str(kind),
        }
        for key, value in fields.items():
            record[str(key)] = _clean(value)

        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Append and flush, no fsync. See the module docstring: this file is
        # allowed to lose its tail in a power cut. state.json is not.
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            f.flush()
        return True
    except Exception:
        return False


def read(kind: str = None, since=None, limit: int = None) -> list:
    """Load events back, newest last. For `events`, doctor, and the tests.

    A malformed line is skipped rather than fatal. The file is appended to by
    a long-running process that can be killed at any moment, so a truncated
    final line is an ordinary thing to find — and one bad line must not make
    the whole history unreadable.
    """
    out = []
    try:
        target = path()
    except Exception:
        return out
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if kind and record.get("kind") != kind:
                    continue
                if since and record.get("ts", "") < since:
                    continue
                out.append(record)
    except FileNotFoundError:
        return []
    except Exception:
        # Deliberately every exception, not just OSError.
        #
        # A path can fail to open for reasons that are not OSError: a null
        # byte in it raises ValueError, and a misconfigured LOG_DIR is exactly
        # how that happens. Caught by tests/test_event_log.py, which pointed
        # config.LOG_DIR at an impossible path and watched this escape.
        #
        # Reading a diagnostic must never be able to raise into `doctor` or
        # `events` — the two commands somebody runs precisely when things are
        # already going wrong.
        return out
    return out[-limit:] if limit else out


def summarise(records: list) -> dict:
    """{kind: count} for a list of events, for a one-line health report."""
    counts = {}
    for record in records:
        kind = record.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
