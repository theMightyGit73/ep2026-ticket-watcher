#!/bin/bash
# Run every test in tests/, and say plainly what failed.
#
#   ./run_tests.sh              # all of them
#   ./run_tests.sh resale       # only tests whose name contains "resale"
#
# The tests are standalone scripts rather than a pytest suite, deliberately:
# each one is runnable on its own with `.venv/bin/python tests/test_x.py` and
# prints a PASS/FAIL line per assertion, so a failure is readable without a
# framework in the way. What was missing was a way to run all of them at once
# — before this, the only way was to remember, and nothing in CI ran them at
# all.
#
# Exit code is the number of failing FILES, capped at 125, so this is usable
# as a gate in a workflow or a pre-push hook.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER="${1:-}"

# Prefer the project venv, fall back to whatever python is on PATH — the
# workflow installs into the runner's own interpreter and has no .venv.
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# The same reason run_watcher.sh sets it: the stock macOS Python links
# against LibreSSL and urllib3 prints a warning on every import, which would
# bury a real traceback in the output of a 38-file run.
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::Warning}"
# Never let a test touch the live state file, the live profile, or the real
# diagnostics directory. Most tests already point these somewhere temporary,
# but "most" is not a guarantee worth betting the running watcher on.
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
export EP_STATE_FILE="$SANDBOX/state.json"
export EP_DIAG_DIR="$SANDBOX/diagnostics"
export EP_LOG_DIR="$SANDBOX/logs"

passed=0
failed=0
failures=()

for test in "$REPO"/tests/test_*.py; do
    name="$(basename "$test")"
    if [ -n "$FILTER" ] && [[ "$name" != *"$FILTER"* ]]; then
        continue
    fi
    if output="$("$PY" "$test" 2>&1)"; then
        passed=$((passed + 1))
        printf '  \033[32mPASS\033[0m  %s\n' "$name"
    else
        failed=$((failed + 1))
        failures+=("$name")
        printf '  \033[31mFAIL\033[0m  %s\n' "$name"
        # Only the failing lines and any traceback — a full dump of 38 files
        # is unreadable, and the PASS lines are not why you are here.
        echo "$output" | grep -E "FAIL|Error|Traceback|^  File |Exception" | sed 's/^/          /'
    fi
done

echo
if [ "$failed" -eq 0 ]; then
    echo "  All $passed test file(s) passed."
    exit 0
fi
echo "  $failed of $((passed + failed)) test file(s) FAILED:"
for name in "${failures[@]}"; do
    echo "    · $name"
    echo "      re-run alone:  $PY tests/$name"
done
exit $(( failed > 125 ? 125 : failed ))
