"""Review state machine + ledger precedence (DESIGN §17/§27.3, DR-003).

Freezes two things:

1. The **state machines** — which review-state transitions are legal for each
   reviewable kind. These are §17 verbatim; anything not listed is illegal.
2. The **ledger precedence rule** (§27.3) — when several ledger entries exist
   for the same target_key: only same-``fingerprint_version`` entries apply
   (DR-007: never mis-apply keys minted under different normalization rules);
   manual (curator) decisions outrank ``auto`` ones; ties resolve to the
   latest ``decided_at``.

Re-decisions over time are ledger entries (precedence), not state-machine
transitions — the state machine governs a single build's review lifecycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from core.resolve.fingerprints import FINGERPRINT_VERSION

# --- frozen vocabularies (§4/§17) -------------------------------------------

LEDGER_TARGET_KINDS = ("entity", "relation", "merge")
LEDGER_DECISIONS = ("approve", "reject", "defer", "merge", "split")

#: ``decided_by`` value marking a pipeline decision; anything else is a curator.
AUTO_DECIDER = "auto"

# --- state machines (§17) ----------------------------------------------------

_ENTITY_RELATION_REVIEW: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # §4/§17 expose the pending state under two vocabularies: the lifecycle
        # `status` field says `needs_review`, the `review_status` field starts
        # at `unreviewed`. Both name the same pending state, so both are legal
        # from-states — a caller validating either field gets the same answer.
        "unreviewed": frozenset({"approved", "rejected"}),
        "needs_review": frozenset({"approved", "rejected"}),
        "approved": frozenset(),
        "rejected": frozenset(),
    }
)
_MERGE_CANDIDATE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # §17: pending → approved|rejected|deferred; defer keeps it reviewable
        "pending": frozenset({"approved", "rejected", "deferred"}),
        "deferred": frozenset({"approved", "rejected"}),
        "approved": frozenset(),
        "rejected": frozenset(),
    }
)
_ONTOLOGY_PROPOSAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "proposed": frozenset({"accepted", "rejected"}),
        "accepted": frozenset(),
        "rejected": frozenset(),
    }
)

STATE_MACHINES: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "entity_review": _ENTITY_RELATION_REVIEW,
        "relation_review": _ENTITY_RELATION_REVIEW,
        "merge_candidate": _MERGE_CANDIDATE,
        "ontology_proposal": _ONTOLOGY_PROPOSAL,
    }
)


def can_transition(kind: str, current: str, target: str) -> bool:
    """True iff §17 allows moving ``kind`` from ``current`` to ``target``."""
    machine = STATE_MACHINES.get(kind)
    if machine is None:
        raise ValueError(f"unknown reviewable kind: {kind!r}")
    return target in machine.get(current, frozenset())


# --- ledger precedence (§27.3) -----------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """One ledger row for a target_key (storage lands in review_ledger)."""

    decision: str
    decided_by: str
    decided_at: datetime
    fingerprint_version: int = FINGERPRINT_VERSION


def effective_decision(
    entries: list[LedgerEntry],
    *,
    fingerprint_version: int = FINGERPRINT_VERSION,
) -> LedgerEntry | None:
    """Pick the entry that governs a target_key, or None if none applies.

    §27.3 precedence, in order: (1) only entries minted under the requested
    ``fingerprint_version`` apply — others need migration or re-review, never
    silent reuse (DR-007); (2) manual (curator) entries outrank ``auto``;
    (3) within the surviving pool, the latest ``decided_at`` wins.
    """
    applicable = [e for e in entries if e.fingerprint_version == fingerprint_version]
    if not applicable:
        return None
    manual = [e for e in applicable if e.decided_by != AUTO_DECIDER]
    pool = manual or applicable
    return max(pool, key=lambda entry: entry.decided_at)
