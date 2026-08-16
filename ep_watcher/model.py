"""Shared vocabulary for "is there a ticket".

Both sources (the Inventory Status API and the browser) answer in these types,
so the alerting logic never has to care which one produced the answer.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# ── Status values ────────────────────────────────────────────────────────────
# Deliberately mirrors Ticketmaster's own Inventory Status vocabulary so the
# API source maps across with no invention on our part.
AVAILABLE = "AVAILABLE"
FEW_LEFT = "FEW_LEFT"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"

#: Statuses that should wake David up.
GOOD_STATUSES = (AVAILABLE, FEW_LEFT)

#: How much each status is worth when two readings disagree. Combining picks
#: the more confident answer, and on a tie between two definite-but-opposite
#: answers prefers the optimistic one: a false alarm costs a glance at a
#: phone, a miss costs the ticket. The old watcher chose the other asymmetry
#: — "defaulting to sold out to avoid false alerts" — which is the wrong
#: trade for something whose only job is to not miss a drop.
_CONFIDENCE = {UNKNOWN: 0, UNAVAILABLE: 1, FEW_LEFT: 2, AVAILABLE: 2}


def better_status(a: str, b: str) -> str:
    """Combine two answers about the same market into the one worth acting on."""
    if _CONFIDENCE.get(a, 0) > _CONFIDENCE.get(b, 0):
        return a
    if _CONFIDENCE.get(b, 0) > _CONFIDENCE.get(a, 0):
        return b
    return a if a in GOOD_STATUSES else b


@dataclass
class Listing:
    """One purchasable thing we could see — a tier, or a resale listing."""
    name: str
    price: Optional[str] = None
    kind: str = "primary"        # "primary" | "resale"
    locked: bool = False         # needs an unlock/presale code

    def describe(self) -> str:
        bits = [self.name]
        if self.price:
            bits.append(f"— {self.price}")
        if self.locked:
            bits.append("[needs unlock code]")
        return " ".join(bits)


@dataclass
class Reading:
    """One source's answer for one poll.

    `primary` and `resale` are tracked separately and alerted on separately.
    They are genuinely different products here: primary is the box office
    releasing returned stock, resale is another punter relisting a ticket. For
    a sold-out festival two weeks out, resale is the likelier of the two, and
    collapsing both into one boolean is what made the old watcher unable to
    say anything useful about either.
    """
    source: str
    #: Which ticket page this reading is about. Carried on the reading itself
    #: so an alert can never name the wrong event — with more than one page
    #: being watched, "a ticket is available" is useless without saying which.
    event_slug: str = ""
    event_name: str = ""
    event_url: str = ""
    primary: str = UNKNOWN
    resale: str = UNKNOWN
    listings: List[Listing] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    #: Set when the source could not produce a real answer at all (network
    #: failure, bot wall, logged out). Distinct from "answered UNAVAILABLE" —
    #: the watchdog counts these, the alerting logic ignores them.
    failed: bool = False
    #: On a merged reading: the sources that failed while others answered.
    #: Populated by engine.merge(); always empty on a single source's reading.
    failed_sources: List[str] = field(default_factory=list)
    #: The other half of that pair — who did answer. Kept so an alert can say
    #: which sources the reading actually rests on.
    answering_sources: List[str] = field(default_factory=list)
    #: Set when the session needs a human: logged out, or challenged.
    needs_login: bool = False
    #: Set on an HTTP 403 — this client is rate-limited rather than merely
    #: challenged. The watch loop backs off hard on this rather than carrying
    #: on at its normal cadence, which would deepen the block.
    blocked: bool = False

    @property
    def any_good(self) -> bool:
        return self.primary in GOOD_STATUSES or self.resale in GOOD_STATUSES

    @property
    def degraded(self) -> bool:
        """A real answer, but not from everything that was supposed to answer.

        This exists because "some source failed" and "the poll failed" are
        different facts, and collapsing them hid a real outage. The API
        sources answer from anywhere and essentially never fail, so once one
        of them is configured, `failed` can only be true if even it broke —
        and a browser that is 403-blocked for hours reads as a clean run,
        resetting the failure counter every poll and reporting "0 failed" in
        the hourly email. Observed in the logs on 2026-08-14.

        Degradation is the missing middle: keep the data that did arrive, act
        on it, alert on it — and still count the poll as unhealthy, because
        the browser is the only source that can see a resale listing.
        """
        return bool(self.failed_sources) and not self.failed

    def note(self, msg: str) -> "Reading":
        self.notes.append(msg)
        return self

    def summary(self) -> str:
        who = f"{self.event_slug}: " if self.event_slug else ""
        return f"{who}primary={self.primary} resale={self.resale} (via {self.source})"
