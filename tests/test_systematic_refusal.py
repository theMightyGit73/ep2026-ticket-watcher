"""Sixty-five identical failures should say so once, not sixty-five times.

Added 2026-08-24, and the thing it defends is a reporting property rather than
a buying one.

Between 2026-08-20 and 2026-08-24 the watcher attempted to buy 65 resale
listings and secured none. Every attempt was refused at the same step. Every
refusal was explained individually, in prose, in the event log — a lost race,
then somebody else's basket, then a quantity of zero. Each explanation was
plausible for one listing and absurd for sixty-five, and nothing in the system
was watching the sixty-five. The per-attempt emails arrived faithfully and
truthfully for five days while the sentence that mattered — "none of this is
working" — was never said by anything.

Two properties follow, and they are pinned here because both were absent
while the whole suite was green:

  1. A run of refusals of listings Ticketmaster itself calls ACTIVE raises one
     alarm, once, distinct from the per-attempt noise.
  2. No failure message claims the ticket sold when the captured payload says
     it did not. That claim is what filed live tickets as lost races and
     pointed a fortnight of work at shaving seconds off a click.

Run with:  .venv/bin/python tests/test_systematic_refusal.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _sandbox  # noqa: F401,E402  (redirect writes; see tests/_sandbox.py)

from ep_watcher import buyer, config, state as state_mod  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


print("\nA run of refused-but-live listings trips the alarm exactly once")

st = state_mod.blank_state() if hasattr(state_mod, "blank_state") else {}
tripped = []
for i in range(1, config.SECURE_REFUSAL_STREAK + 4):
    if state_mod.note_secure_refusal(st, True):
        tripped.append(i)

check("it fires on the threshold attempt", tripped, [config.SECURE_REFUSAL_STREAK])
check("and never again while the run continues", len(tripped), 1)
check("the streak keeps counting past it",
      st["secure_refusal_streak"] >= config.SECURE_REFUSAL_STREAK + 3, True)


print("\nOne success resets it")

state_mod.note_secure_refusal(st, False)
check("the count goes back to zero", st["secure_refusal_streak"], 0)
check("and the alarm is re-armed", st["secure_refusal_alerted"], None)

# A second run must be able to raise a second alarm. Latching this the way the
# old watchdog latched its "already alerted" flag is how 44 days of silence
# happened on the previous incarnation of this project.
tripped = []
for i in range(1, config.SECURE_REFUSAL_STREAK + 1):
    if state_mod.note_secure_refusal(st, True):
        tripped.append(i)
check("a later run alarms again", tripped, [config.SECURE_REFUSAL_STREAK])


print("\nA short run stays quiet")

st2 = {}
quiet = [state_mod.note_secure_refusal(st2, True)
         for _ in range(config.SECURE_REFUSAL_STREAK - 1)]
check("one below the threshold says nothing", any(quiet), False)


print("\nThe threshold is a pattern, not an incident")

# One refusal is routine and two is bad luck. It also has to trip fast enough
# to matter: at roughly thirteen listings a day this must alarm within hours
# of a regression, not after somebody reads five days of logs end to end.
check("more than a couple", config.SECURE_REFUSAL_STREAK >= 3, True)
check("but reachable within a day's listings",
      config.SECURE_REFUSAL_STREAK <= 10, True)


print("\nNo message claims a sale the evidence contradicts")

# The exact shape that misled the project: Ticketmaster refuses, and its own
# error payload says the listing is live. "It sold" is not a conclusion
# available to us there — it is contradicted by the party that would know, at
# the moment of refusal.
result = buyer.HoldResult()
result.ever_active = True
result.secured = False
result.attempts = 3
result.cache_replays = 2
result.listing_id = "l0vmtvwkd2"
result.offer_summary = "General Admission Tier 2 Ticket · section STNDN1 · €310.50"

reason = buyer.active_refusal_reason(result)

# What must never appear is a claim of sale, or a named cause presented as
# fact. Both were in the shipped wording and both were wrong.
for banned in ("had genuinely sold", "it sold", "was sold"):
    check(f"the active-listing reason never says {banned!r}",
          banned in reason, False)

# The specific inference that cost twelve minutes a listing for a day.
check("nor asserts a basket as the cause",
      "sitting in somebody else's basket" in reason, False)

# It must say the thing that is true and useful instead.
check("it says the listing was active", "ACTIVE" in reason, True)
check("it admits the cause is unknown", "NOT established" in reason, True)
check("and it tells David to buy by hand", "by hand" in reason, True)

# A chase whose retries were cache replays must confess that, or the count of
# attempts reads as evidence it is not.
check("replays are disclosed when there were any",
      "never reached Ticketmaster" in reason, True)

clean = buyer.HoldResult()
clean.ever_active = True
clean.attempts = 3
check("and not mentioned when there were none",
      "never reached Ticketmaster" in buyer.active_refusal_reason(clean), False)


print("\nThe chase cannot occupy the buying browser for a quarter of an hour")

# Ten chases ran the full twelve-minute window on 2026-08-24, 114 minutes in
# total, a fifth of the day — and the watchdog deferred six restarts because
# it could not tell a chase from a hang. The ceiling is what stops that, so it
# is pinned rather than left to whoever next edits a constant.
budget = config.secure_budget_seconds()
check("the whole budget is at most three minutes", budget <= 180, True)
check("and long enough for a few real attempts", budget >= 90, True)
check("the active-listing window no longer waits out a basket",
      config.SECURE_ACTIVE_TIMEOUT_SECONDS <= 180, True)
check("and takes a small number of goes",
      config.SECURE_ACTIVE_RETRIES <= 4, True)


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All systematic-refusal checks passed.")
