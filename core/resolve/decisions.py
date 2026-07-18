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
    """A §17 review state machine refuses this move (e.g. deciding an
    already-decided target). Carries what a client needs to see why —
    ``subject`` names the reviewable kind ('merge candidate'/'entity'/
    'relation'), ``current``/``verb`` drive the 400 details."""

    def __init__(self, subject: str, subject_id: uuid.UUID, current: str, verb: str) -> None:
        super().__init__(
            f"{subject} {subject_id} is {current!r} — {verb!r} is not a legal §17 "
            "transition (approved/rejected are terminal)"
        )
        self.subject = subject
        self.subject_id = subject_id
        self.current = current
        self.verb = verb


class EntityNotFoundError(Exception):
    """No such entity in this project's given build."""

    def __init__(self, project: str, entity_id: uuid.UUID) -> None:
        super().__init__(f"entity {entity_id} not found in project {project!r}")
        self.project = project
        self.entity_id = entity_id


class RelationNotFoundError(Exception):
    """No such relation in this project's given build."""

    def __init__(self, project: str, relation_id: uuid.UUID) -> None:
        super().__init__(f"relation {relation_id} not found in project {project!r}")
        self.project = project
        self.relation_id = relation_id


#: entity/relation curator verbs (no defer — §17 has only approve|reject there).
_REVIEW_VERB_TO_STATUS = {"approve": "approved", "reject": "rejected"}


def _require_curator_decision(verb: str, decided_by: str) -> None:
    """Shared guard for the entity/relation decide path: a known verb and a
    non-empty curator principal that does not impersonate the pipeline (§27.3
    precedence keys off ``decided_by``)."""
    if verb not in _REVIEW_VERB_TO_STATUS:
        raise ValueError(
            f"unknown decision verb {verb!r} (choose from {sorted(_REVIEW_VERB_TO_STATUS)})"
        )
    if decided_by == AUTO_DECIDER or not decided_by:
        raise ValueError(
            f"decided_by {decided_by!r} is reserved/empty — curator entries must not "
            "impersonate the pipeline (§27.3 precedence keys off it)"
        )


def _review_row_values(verb: str, current_status: str) -> dict[str, str]:
    """The ``status``/``review_status`` an entity/relation row takes on a
    curator decision — MIRRORS ``core.resolve.resolution``'s build-time
    application so the ACTIVE build's row shows exactly what the NEXT build
    re-derives from the ledger. reject excludes (both → rejected); approve marks
    review_status=approved and RESTORES status to active iff the row was
    excluded (rejected/needs_review), else leaves the lifecycle status alone."""
    if verb == "reject":
        return {"status": "rejected", "review_status": "rejected"}
    if current_status in ("rejected", "needs_review"):
        return {"status": "active", "review_status": "approved"}
    return {"review_status": "approved"}


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
        raise InvalidReviewTransitionError("merge candidate", candidate_id, row.status, verb)

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


_ENTITY_REVIEW_COLS = (
    tables.entities.c.id,
    tables.entities.c.project,
    tables.entities.c.build_id,
    tables.entities.c.type,
    tables.entities.c.canonical_name,
    tables.entities.c.entity_key,
    tables.entities.c.attributes,
    tables.entities.c.status,
    tables.entities.c.review_status,
    tables.entities.c.created_by,
    tables.entities.c.created_at,
    tables.entities.c.updated_at,
)

_RELATION_REVIEW_COLS = (
    tables.relations.c.id,
    tables.relations.c.project,
    tables.relations.c.build_id,
    tables.relations.c.src_entity_id,
    tables.relations.c.dst_entity_id,
    tables.relations.c.type,
    tables.relations.c.attributes,
    tables.relations.c.relation_signature,
    tables.relations.c.status,
    tables.relations.c.review_status,
    tables.relations.c.created_by,
    tables.relations.c.confidence,
    tables.relations.c.created_at,
    tables.relations.c.updated_at,
)


async def decide_entity(
    conn: AsyncConnection,
    *,
    project: str,
    build_id: uuid.UUID,
    entity_id: uuid.UUID,
    verb: str,
    decided_by: str,
    reason: str | None = None,
) -> sa.Row[Any]:
    """Record a curator's approve/reject on an entity (GOV2, §17).

    Two writes in the caller's transaction: (1) the non-build-scoped
    ``review_ledger`` CARRY-FORWARD entry keyed by the TYPE-FREE v2
    ``ledger_entity_key`` (DR-011) — the SAME key ``resolve_build`` re-mints
    from ``(canonical_name, disambiguator)``, so the decision survives the
    entity being re-typed and applies on every future build (reject → excluded
    from the projection, approve → adopted); (2) the ACTIVE build's row records
    the resulting ``status``/``review_status`` so inspect views reflect it now.
    The row is locked ``FOR UPDATE`` and the §17 transition checked under the
    lock (two concurrent decides serialize — the loser gets a typed refusal, not
    a double decision). Does NOT commit. Raises ``EntityNotFoundError``,
    ``InvalidReviewTransitionError``, or ``ValueError`` (bad verb / reserved
    ``decided_by``)."""
    _require_curator_decision(verb, decided_by)
    ents = tables.entities
    row = (
        await conn.execute(
            sa.select(
                ents.c.canonical_name, ents.c.disambiguator, ents.c.status, ents.c.review_status
            )
            .where(ents.c.id == entity_id, ents.c.project == project, ents.c.build_id == build_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise EntityNotFoundError(project, entity_id)
    target = _REVIEW_VERB_TO_STATUS[verb]
    # validate on review_status (the §17 field): unreviewed is decidable,
    # approved/rejected are terminal. `status` may be active/needs_review/… and
    # is NOT the §17 gate — the machine accepts unreviewed as the pending state.
    if not can_transition("entity_review", row.review_status, target):
        raise InvalidReviewTransitionError("entity", entity_id, row.review_status, verb)

    target_key = fingerprints.ledger_entity_key(row.canonical_name, row.disambiguator)
    decided_at = (await conn.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()
    await conn.execute(
        tables.review_ledger.insert().values(
            project=project,
            target_kind="entity",
            target_key=target_key,
            fingerprint_version=fingerprints.LEDGER_FINGERPRINT_VERSION,
            decision=verb,  # the raw verb — resolve's _decision branches on it
            decided_by=decided_by,
            decided_at=decided_at,
            reason=reason,
        )
    )
    updated = (
        await conn.execute(
            ents.update()
            .where(ents.c.id == entity_id)
            .values(**_review_row_values(verb, row.status), updated_at=decided_at)
            .returning(*_ENTITY_REVIEW_COLS)
        )
    ).one()
    return updated


async def decide_relation(
    conn: AsyncConnection,
    *,
    project: str,
    build_id: uuid.UUID,
    relation_id: uuid.UUID,
    verb: str,
    decided_by: str,
    reason: str | None = None,
) -> sa.Row[Any]:
    """Record a curator's approve/reject on a relation (GOV2, §17).

    Same shape as :func:`decide_entity`, but the type-free v2 ledger key is the
    ``ledger_relation_signature`` over the ENDPOINTS' ledger keys — so the
    decision first resolves ``src_entity_id``/``dst_entity_id`` to their
    ``ledger_entity_key`` (exactly what ``resolve_build`` does), then mints
    ``ledger_relation_signature(src_key, type, dst_key)``. That key is minted
    from the endpoints, NOT the row's ``relation_signature`` column, so a row
    whose signature is still NULL (pre-resolve) is still decided correctly — the
    entry simply applies on the next build once the relation carries a
    signature. Raises ``RelationNotFoundError``,
    ``InvalidReviewTransitionError``, ``ValueError``, or ``LookupError`` (an
    endpoint entity is unmintable — unreachable behind the composite FK)."""
    _require_curator_decision(verb, decided_by)
    rels = tables.relations
    row = (
        await conn.execute(
            sa.select(
                rels.c.src_entity_id,
                rels.c.dst_entity_id,
                rels.c.type,
                rels.c.status,
                rels.c.review_status,
            )
            .where(rels.c.id == relation_id, rels.c.project == project, rels.c.build_id == build_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise RelationNotFoundError(project, relation_id)
    target = _REVIEW_VERB_TO_STATUS[verb]
    if not can_transition("relation_review", row.review_status, target):
        raise InvalidReviewTransitionError("relation", relation_id, row.review_status, verb)

    ents = tables.entities
    keys = {
        r.id: fingerprints.ledger_entity_key(r.canonical_name, r.disambiguator)
        for r in await conn.execute(
            sa.select(ents.c.id, ents.c.canonical_name, ents.c.disambiguator).where(
                ents.c.id.in_([row.src_entity_id, row.dst_entity_id]),
                ents.c.project == project,
                ents.c.build_id == build_id,
            )
        )
    }
    if set(keys) != {row.src_entity_id, row.dst_entity_id}:
        # the composite FK makes this unreachable; if it fires, the endpoints'
        # ledger identity is unmintable and recording the decision would be a lie
        raise LookupError(
            f"relation {relation_id}'s endpoints are missing from build {build_id} "
            "— cannot mint the §27.3 relation ledger key"
        )
    target_key = fingerprints.ledger_relation_signature(
        keys[row.src_entity_id], row.type, keys[row.dst_entity_id]
    )
    decided_at = (await conn.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()
    await conn.execute(
        tables.review_ledger.insert().values(
            project=project,
            target_kind="relation",
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
            rels.update()
            .where(rels.c.id == relation_id)
            .values(**_review_row_values(verb, row.status), updated_at=decided_at)
            .returning(*_RELATION_REVIEW_COLS)
        )
    ).one()
    return updated
