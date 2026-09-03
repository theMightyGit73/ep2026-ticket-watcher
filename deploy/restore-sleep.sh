#!/bin/bash
# Put the Mac's power settings back to Apple's defaults.
#
#   sudo ./deploy/restore-sleep.sh
#
# The watcher needed the Mac awake to work, so setup asked for:
#
#     sudo pmset -a sleep 0 disablesleep 1
#
# That is a MANUAL change made outside the service, so stopping the watcher
# does not undo it — it outlived the 2026 run by five days. With
# `disablesleep 1` the Mac cannot sleep at all, including on battery and
# including when the lid is shut, which means fans, heat and a flat battery
# for no reason once the watcher has finished.
#
# `restoredefaults` is used rather than a list of numbers so this cannot
# encode somebody's guess at what "normal" looks like — it asks macOS for its
# own defaults. `disablesleep` is cleared explicitly because it is a separate
# lock and is not covered by that reset.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This needs sudo:  sudo $0" >&2
    exit 1
fi

echo "==> Before"
pmset -g | grep -Ei "SleepDisabled|displaysleep|^ *sleep" || true

echo
echo "==> Clearing the sleep lock"
pmset -a disablesleep 0

echo "==> Restoring Apple's defaults"
pmset restoredefaults

echo
echo "==> After"
pmset -g | grep -Ei "SleepDisabled|displaysleep|^ *sleep" || true

echo
if pmset -g | grep -qE "SleepDisabled[[:space:]]+1"; then
    echo "STILL LOCKED — SleepDisabled is 1. Something else is setting it."
    exit 1
fi
echo "OK — the Mac can sleep again."
