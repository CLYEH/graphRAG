"""Curator review decisions over merge candidates (BA5) — the Console's write
half of §17, mirroring C4's read half.

One decision = three writes that must land in ONE caller-owned transaction:

* the candidate row is locked ``FOR UPDATE`` and the §17 transition is checked
  UNDER the lock (class 10 — two concurrent decides serialize here; the loser
  re-reads the new status and gets a typed refusal, never a double decision);
* ``review_ledger`` gains the CARRY-FORWARD entry (DR-003: non-build-scoped,
  keyed by the §27.3 v2 ``ledger_merge_key`` — computed with the SAME
  ``fingerprints.ledger_merge_key`` C4's resolve reads, so key drift is
  structurally impossible). DEFER writes a ledger entry too: #28 R4a froze that a deferred
  pair must not auto-merge, so resolve has to SEE the defer;
* the candidate row records the audit trail (status/decision/decided_by/
  decided_at/reason). Both stamps reuse ONE ``clock_timestamp()`` captured
  per decision — still the single DB clock, and ledger + candidate carry the
  same instant; deliberately NOT ``now()``, which is transaction-stable, so
  two decisions batched in one transaction (defer then approve — legal, §17)
  would TIE on ``decided_at`` and §27.3's latest-wins precedence would
  resolve the pair arbitrarily (Codex #59 R2). ``clock_timestamp()`` is
  per-call with microsecond precision; two sequential decision round-trips
  cannot tie.

``decided_by`` must never be :data:`~core.resolve.review.AUTO_DECIDER` — that
value marks pipeline auto-decisions, and §27.3 precedence lets curators
outrank it; the API passes its §23 placeholder principal instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.resolve import fingerprints
from core.resolve.review import AUTO_DECIDER, can_transition
from core.stores import tables

#: the frozen contract's decision verbs → the §17 target states
_VERB_TO_STATUS = {"approve": "approved", "reject": "rejected", "defer": "deferred"}


@dataclass(frozen=True)
class MergeCandidate:
    """One merge_candidates row, in column order (the contract's field set
    plus the scoping ``project``)."""

    id: uuid.UUID
    project: str
    build_id: uuid.UUID
    left_entity_id: uuid.UUID
    right_entity_id: uuid.UUID
    score: float
    features: dict[str, Any] | None
    status: str
    decision: str | None
    decided_by: str | None
    decided_at: datetime | None
    reason: str | None
    impact: dict[str, Any] | None
    left_snapshot: dict[str, Any] | None
    right_snapshot: dict[str, Any] | None


class MergeCandidateNotFoundError(Exception):
    """No such candidate in this project's given build."""

    def __init__(self, project: str, candidate_id: uuid.UUID) -> None:
        super().__init__(f"merge candidate {candidate_id} not found in project {project!r}")
        self.project = project
        self.candidate_id = candidate_id


class InvalidReviewTransitionError(Exception):
    """The §17 merge-candidate state machine refuses this move (e.g. deciding
    an already-approved candidate). Carries what a client needs to see why."""

    def __init__(self, candidate_id: uuid.UUID, current: str, verb: str) -> None:
        super().__init__(
            f"merge candidate {candidate_id} is {current!r} — {verb!r} is not a legal "
            "§17 transition (pending → approved|rejected|deferred; deferred → "
            "approved|rejected; approved/rejected are terminal)"
        )
        self.candidate_id = candidate_id
        self.current = current
        self.verb = verb


_COLS = (
    tables.merge_candidates.c.id,
    tables.merge_candidates.c.project,
    tables.merge_candidates.c.build_id,
    tables.merge_candidates.c.left_entity_id,
    tables.merge_candidates.c.right_entity_id,
    tables.merge_candidates.c.score,
    tables.merge_candidates.c.features,
    tables.merge_candidates.c.status,
    tables.merge_candidates.c.decision,
    tables.merge_candidates.c.decided_by,
    tables.merge_candidates.c.decided_at,
    tables.merge_candidates.c.reason,
    tables.merge_candidates.c.impact,
    tables.merge_candidates.c.left_snapshot,
    tables.merge_candidates.c.right_snapshot,
)


async def decide_merge_candidate(
    conn: AsyncConnection,
    *,
    project: str,
    build_id: uuid.UUID,
    candidate_id: uuid.UUID,
    verb: str,
    decided_by: str,
    reason: str | None = None,
) -> MergeCandidate:
    """Record a curator's decision — see the module docstring for the shape.

    Raises ``MergeCandidateNotFoundError`` (absent in this project+build),
    ``InvalidReviewTransitionError`` (§17 refusal), or ``ValueError`` for a
    verb outside the frozen vocabulary or an ``AUTO_DECIDER`` curator name.
    Does NOT commit — the caller owns the transaction (the registry pattern).
    """
    if verb not in _VERB_TO_STATUS:
        raise ValueError(f"unknown decision verb {verb!r} (choose from {sorted(_VERB_TO_STATUS)})")
    if decided_by == AUTO_DECIDER or not decided_by:
        raise ValueError(
            f"decided_by {decided_by!r} is reserved/empty — curator entries must not "
            "impersonate the pipeline (§27.3 precedence keys off it)"
        )

    mc = tables.merge_candidates
    row = (
        await conn.execute(
            sa.select(*_COLS)
            .where(mc.c.id == candidate_id, mc.c.project == project, mc.c.build_id == build_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise MergeCandidateNotFoundError(project, candidate_id)
    target = _VERB_TO_STATUS[verb]
    if not can_transition("merge_candidate", row.status, target):
        raise InvalidReviewTransitionError(candidate_id, row.status, verb)

    ents = tables.entities
    keys = {
        r.id: fingerprints.ledger_entity_key(r.canonical_name, r.disambiguator)
        for r in await conn.execute(
            sa.select(ents.c.id, ents.c.canonical_name, ents.c.disambiguator).where(
                ents.c.id.in_([row.left_entity_id, row.right_entity_id]),
                ents.c.project == project,
                ents.c.build_id == build_id,
            )
        )
    }
    if set(keys) != {row.left_entity_id, row.right_entity_id}:
        # the composite FK makes this unreachable; if it ever fires, the pair
        # identity is unmintable and recording a decision would be a lie
        raise LookupError(
            f"merge candidate {candidate_id}'s entities are missing from "
            f"build {build_id} — cannot mint the §27.3 merge ledger key"
        )
    # the TYPE-FREE v2 ledger key (DR-011): the curator's「這兩個是同一個東西」
    # must survive either side being re-typed by the next build's extraction
    target_key = fingerprints.ledger_merge_key(keys[row.left_entity_id], keys[row.right_entity_id])

    # ONE per-decision instant for both writes (module docstring: now() would
    # tie batched re-decisions and break §27.3's latest-wins tie-break)
    decided_at = (await conn.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()
    await conn.execute(
        tables.review_ledger.insert().values(
            project=project,
            target_kind="merge",
            target_key=target_key,
            fingerprint_version=fingerprints.LEDGER_FINGERPRINT_VERSION,
            decision=verb,
            decided_by=decided_by,
            decided_at=decided_at,
            reason=reason,
        )
    )
    updated = (
        await conn.execute(
            mc.update()
            .where(mc.c.id == candidate_id)
            .values(
                status=target,
                decision=verb,
                decided_by=decided_by,
                decided_at=decided_at,
                reason=reason,
            )
            .returning(*_COLS)
        )
    ).one()
    return MergeCandidate(*updated)
