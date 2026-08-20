"""A record of the work that can be queried, and can never stop the work.

watcher.log is written for a person reading it at three in the morning and is
good at that. It is bad at everything else. On 2026-08-20 three throwaway
parsers were written against it to answer basic questions, and one of them
reported nearly every poll as a find because `"AVAILABLE" in "UNAVAILABLE"` is
true. The answer was wrong and looked entirely plausible — which is the whole
argument for having data to query instead of prose.

Two properties matter here and they pull against each other. The log has to be
complete enough to answer real questions, and it has to be incapable of
costing a poll. Most of what follows tests the second: a diagnostic that can
throw is worse than no diagnostic, because it fails at the moment the system
is already having a bad day.

Run with:  .venv/bin/python tests/test_event_log.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, events  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


with tempfile.TemporaryDirectory() as tmp:
    was = config.LOG_DIR
    config.LOG_DIR = Path(tmp)
    try:
        print("\nOne line per event, and it reads back as it was written")
        check("an empty log reads as nothing", events.read(), [])
        check_true("a find is written",
                   events.emit("find", event="weekend-camping", via="sweep",
                               price="€366.39", listing_ids=["l5mm1z9t1s"]))
        events.emit("poll", event="weekend-camping", resale="UNAVAILABLE")
        events.emit("poll", event="early-entry", resale="UNKNOWN")
        back = events.read()
        check("all three came back", len(back), 3)
        check("in the order they happened", [r["kind"] for r in back],
              ["find", "poll", "poll"])
        check("with the euro sign intact", back[0]["price"], "€366.39")
        check("and lists preserved", back[0]["listing_ids"], ["l5mm1z9t1s"])
        check_true("every record is stamped", all("ts" in r for r in back))

        print("\nIt is really one JSON object per line")
        # The point of JSONL is that ordinary tools work on it. If a record
        # ever spans lines, every `grep | jq` anyone writes later breaks.
        raw = events.path().read_text().strip().splitlines()
        check("three lines for three events", len(raw), 3)
        check_true("each parses on its own",
                   all(json.loads(line)["kind"] for line in raw))

        print("\nFiltering, which is the reason it exists")
        check("by kind", len(events.read(kind="poll")), 2)
        check("by kind, the other one", len(events.read(kind="find")), 1)
        check("a kind nobody wrote", events.read(kind="nonsense"), [])
        check("and a tail limit", len(events.read(limit=2)), 2)
        check("counts by kind", events.summarise(events.read()),
              {"poll": 2, "find": 1})

        print("\nA truncated final line must not lose the whole history")
        # The writer is a long-running process that can be killed at any
        # moment — the watchdog's repair is literally a kill — so a half
        # written last line is an ordinary thing to find, not a corruption.
        with open(events.path(), "a") as f:
            f.write('{"ts": "2026-08-20T12:00:00+00:00", "kind": "po')
        survivors = events.read()
        check("the complete records still read", len(survivors), 3)
        check_true("and the broken one is simply skipped",
                   all(r["kind"] in ("find", "poll") for r in survivors))

        print("\nNothing a caller passes can make it raise")
        # This is the property that matters most. emit() is called from inside
        # the poll loop, and a diagnostic that throws would take down the
        # thing it was documenting.
        class Awkward:
            def __repr__(self):
                raise RuntimeError("not even repr works")

        cases = {
            "an object that cannot be repr'd": {"bad": Awkward()},
            "a set, which json cannot encode": {"s": {1, 2, 3}},
            "a nested structure": {"d": {"a": [1, {"b": (2, 3)}]}},
            "a None": {"nothing": None},
            "no fields at all": {},
        }
        for label, fields in cases.items():
            try:
                events.emit("stress", **fields)
                raised = False
            except Exception:
                raised = True
            check(f"survives {label}", raised, False)

        print("\nAnd an unwritable log is a shrug, not a crash")
        config.LOG_DIR = Path(tmp) / "no" / "such" / "\x00bad"
        try:
            wrote = events.emit("find", event="x")
            raised = False
        except Exception:
            raised, wrote = True, None
        check("emit does not raise on an impossible path", raised, False)
        check("and honestly reports it did not write", wrote, False)
        check("reading an absent log is empty, not an error", events.read(), [])
        config.LOG_DIR = Path(tmp)

        print("\nA runaway value cannot fill the disk")
        # The machine that has to stay responsive enough to buy a ticket is
        # the same machine this writes to.
        events.emit("huge", page="x" * 100_000)
        biggest = max(len(line) for line in events.path().read_text().splitlines())
        check_true(f"the longest line is bounded ({biggest} chars)",
                   biggest < events.MAX_VALUE_CHARS + 500)
        check_true("and it is still valid JSON",
                   json.loads(events.path().read_text().splitlines()[-1])["kind"] == "huge")
    finally:
        config.LOG_DIR = was

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
