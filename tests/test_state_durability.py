"""The state file must survive being killed halfway through writing it.

state.json is the watcher's whole memory: which listings it has already
alerted on, how many blocks this connection has drawn, when the next poll is
due, and — since 2026-08-19 — whether a ticket is being held right now.

It used to be written with a plain `open(path, "w")`, which truncates the real
file before a single byte of the new content exists. A process killed inside
that window leaves an empty or half-written file, and load() swallowed the
parse error and started over from defaults without a word.

That window is not hypothetical in this project. The watchdog's repair is
`launchctl kickstart -k`, which is a kill, and this file is rewritten on every
cycle and every thirty seconds while a checkout is open. The failure feeds
itself: among the things lost is `hold_until`, the marker that tells the
watchdog not to kill a live checkout — so the kill destroys the evidence that
the kill was wrong.

Run with:  .venv/bin/python tests/test_state_durability.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import config, state as st  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    was = config.STATE_FILE
    config.STATE_FILE = tmp / "state.json"
    try:
        print("\nAn ordinary round trip")
        state = dict(st._defaults())
        state["checks_total"] = 7
        st.note_hold(state, 15)
        st.save(state)
        back = st.load()
        check("what went in comes back", back["checks_total"], 7)
        check_true("including the hold marker", st.hold_remaining(back) > 0)
        check("and no temporary file is left lying about",
              sorted(p.name for p in tmp.iterdir()), ["state.json"])

        print("\nA save that dies halfway leaves the PREVIOUS state intact")
        # The property that matters. Simulated by making the serialisation
        # itself fail after the file has been opened — which is precisely the
        # moment a truncating write has already destroyed the old contents.
        real_dump = json.dump

        def dies_halfway(obj, fp, **kw):
            fp.write('{"checks_total": 99, "partial":')   # a real fragment
            raise OSError("killed mid-write")

        try:
            json.dump = dies_halfway
            st.save({"checks_total": 99})
        finally:
            json.dump = real_dump

        survivor = st.load()
        check("the old state is still there, whole", survivor["checks_total"], 7)
        check_true("and its hold marker survived too",
                   st.hold_remaining(survivor) > 0)
        check("with no wreckage beside it",
              sorted(p.name for p in tmp.iterdir()), ["state.json"])
        # And the file on disk is genuinely parseable, not merely re-defaulted
        # by load() swallowing an error.
        check("the file itself parses",
              json.loads(config.STATE_FILE.read_text())["checks_total"], 7)

        print("\nA file that IS corrupt is reported, not silently forgotten")
        config.STATE_FILE.write_text('{"checks_total": 7, "truncated"')
        recovered = st.load()
        check("load falls back to defaults", recovered["checks_total"], 0)
        check("and no hold is invented out of the wreckage",
              st.hold_remaining(recovered), 0.0)

        print("\nA missing file is ordinary and stays silent")
        config.STATE_FILE.unlink()
        fresh = st.load()
        check("defaults, no drama", fresh["checks_total"], 0)
        check("hold_until starts empty", fresh.get("hold_until"), None)

        print("\nA save that cannot possibly work still never raises")
        # run_once() saves in a `finally`, so anything raised here escapes
        # from the middle of a poll and masks what the poll was actually
        # doing. The watch loop above would then blame the browser and
        # cold-restart Chrome once per cycle, chasing a fault in a file.
        # Losing the memory of one poll is cheap; losing the poll is not.
        config.STATE_FILE = tmp / "state.json"
        # An earlier check deleted the file; put a known one back so "the
        # previous state survives" is a claim about something real.
        st.save({"checks_total": 3})

        class Unserialisable:
            pass

        try:
            st.save({"something": Unserialisable()})
            check("a value that will not serialise is swallowed", True, True)
        except Exception as exc:
            check(f"save raised {type(exc).__name__}", False, True)
        check("and the previous state is still on disk, untouched",
              json.loads(config.STATE_FILE.read_text()).get("checks_total"), 3)

        config.STATE_FILE = tmp / "no" / "such" / "\0path" / "state.json"
        try:
            st.save(dict(st._defaults()))
            check("an impossible path is swallowed too", True, True)
        except Exception as exc:
            check(f"save raised {type(exc).__name__}", False, True)
    finally:
        config.STATE_FILE = was

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
