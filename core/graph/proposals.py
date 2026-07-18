"""Ontology proposal pool: persist LLM-proposed types for review (§6 待審池, C3c).

Document extraction (C3b) holds out every out-of-ontology type as a
:class:`~core.graph.documents.TypeProposal`; this module gives those a
persistent home. The pool is deliberately NOT build-scoped (a review
artifact, like the review_ledger): one row per ``(project, proposal_key)``
where ``proposal_key = fpv(norm(kind)|norm(type_name))`` (§27.3 conventions,
DR-007 versioned). Carry-forward is therefore structural — re-proposing an
already-pooled type is an upsert no-op that PRESERVES the existing status, so
a rejected type never re-opens review (DR-003's intent without extending the
ledger's frozen three-kind vocabulary).

🔧 ``ontology.proposal_policy`` (§6):

- ``review`` (default): new proposals land as ``proposed`` and wait for the
  Console (§17: proposed → accepted|rejected — ``core.resolve.review`` owns
  the transition rules).
- ``auto``: new proposals land directly as ``accepted``, decided_by
  ``auto-policy`` — adoption without review is exactly what the policy means;
  this is an INITIAL state, not a transition, so the §17 machine (which
  governs transitions of pooled rows) is not bypassed. Already-pooled rows
  keep their status either way — auto never overturns a human rejection.

Writes take a raw ``AsyncConnection`` because the pool is outside DR-006's
build-scoped world (the repo layer rejects non-build-scoped tables loudly);
this module is the pool's single write path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from core.graph.documents import TypeProposal
from core.registry.store import ProjectNotFoundError
from core.resolve import fingerprints
from core.resolve.review import AUTO_DECIDER, can_transition
from core.stores import tables

#: 🔧 ontology.proposal_policy vocabulary (§6) — a typo'd policy must fail
#: loudly, not silently behave like one of the real values.
PROPOSAL_POLICIES = ("review", "auto")

#: decided_by for auto-policy adoptions (parallel to review.AUTO_DECIDER).
AUTO_POLICY_DECIDER = "auto-policy"


@dataclass(frozen=True)
class ProposalPoolReport:
    """What one persist pass did: rows newly pooled vs already present."""

    pooled: int
    already_present: int


async def persist_proposals(
    conn: AsyncConnection,
    project: str,
    proposals: Iterable[TypeProposal],
    *,
    policy: str = "review",
) -> ProposalPoolReport:
    """Upsert extraction's held-out proposals into the project's pool.

    In-batch duplicates collapse to the first occurrence (its example and
    chunk_ref are the observed evidence); already-pooled keys are untouched
    (ON CONFLICT DO NOTHING) so existing statuses — including rejections —
    survive every rebuild.
    """
    if policy not in PROPOSAL_POLICIES:
        raise ValueError(
            f"unknown ontology.proposal_policy {policy!r} — expected one of {PROPOSAL_POLICIES}"
        )
    if not project.strip():
        raise ValueError("project must be non-empty")

    now = datetime.now(tz=UTC)
    batch: dict[str, TypeProposal] = {}
    for proposal in proposals:
        key = fingerprints.proposal_key(proposal.kind, proposal.type_name)
        batch.setdefault(key, proposal)

    pooled = 0
    for key, proposal in batch.items():
        row: dict[str, object] = {
            "id": uuid.uuid4(),
            "project": project,
            "kind": proposal.kind,
            "type_name": proposal.type_name,
            "proposal_key": key,
            "fingerprint_version": fingerprints.FINGERPRINT_VERSION,
            "example": proposal.example,
            "chunk_ref": proposal.chunk_ref,
            "created_at": now,
        }
        if policy == "auto":
            row |= {
                "status": "accepted",
                "decided_by": AUTO_POLICY_DECIDER,
                "decided_at": now,
                "reason": "ontology.proposal_policy=auto",
            }
        else:
            row["status"] = "proposed"
        statement = (
            pg_insert(tables.ontology_proposals)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["project", "proposal_key"])
        )
        result = await conn.execute(statement)
        pooled += result.rowcount or 0

    return ProposalPoolReport(pooled=pooled, already_present=len(batch) - pooled)


# --- GOV3: Console review of the pool (§17 proposed → accepted|rejected) -----


@dataclass(frozen=True)
class OntologyProposal:
    """One ``ontology_proposals`` row, in column order — the contract
    OntologyProposal field set plus the scoping ``project``."""

    id: uuid.UUID
    project: str
    kind: str
    type_name: str
    proposal_key: str
    fingerprint_version: int
    example: str | None
    chunk_ref: str | None
    status: str
    decided_by: str | None
    decided_at: datetime | None
    reason: str | None
    created_at: datetime


_PROPOSAL_COLS = (
    tables.ontology_proposals.c.id,
    tables.ontology_proposals.c.project,
    tables.ontology_proposals.c.kind,
    tables.ontology_proposals.c.type_name,
    tables.ontology_proposals.c.proposal_key,
    tables.ontology_proposals.c.fingerprint_version,
    tables.ontology_proposals.c.example,
    tables.ontology_proposals.c.chunk_ref,
    tables.ontology_proposals.c.status,
    tables.ontology_proposals.c.decided_by,
    tables.ontology_proposals.c.decided_at,
    tables.ontology_proposals.c.reason,
    tables.ontology_proposals.c.created_at,
)

#: the contract's proposal verbs → the §17 target states.
_PROPOSAL_VERB_TO_STATUS = {"accept": "accepted", "reject": "rejected"}


class OntologyProposalNotFoundError(LookupError):
    """No such proposal in this project's pool."""

    def __init__(self, project: str, proposal_id: uuid.UUID) -> None:
        super().__init__(f"ontology proposal {proposal_id} not found in project {project!r}")
        self.project = project
        self.proposal_id = proposal_id


class InvalidProposalTransitionError(Exception):
    """The §17 ontology-proposal machine refuses this move (e.g. re-deciding an
    already-accepted proposal). Terminal states never re-open."""

    def __init__(self, proposal_id: uuid.UUID, current: str, verb: str) -> None:
        super().__init__(
            f"ontology proposal {proposal_id} is {current!r} — {verb!r} is not a legal "
            "§17 transition (proposed → accepted|rejected; accepted/rejected are terminal)"
        )
        self.proposal_id = proposal_id
        self.current = current
        self.verb = verb


class OntologyConfigIncompleteError(Exception):
    """Accept cannot land the type: the project's configured ontology is
    missing/incomplete, so adding the accepted type alone would leave a block
    the next build rejects. Accept ADDS to an existing valid ontology — it does
    not create one. (Reachable because ``PATCH /projects`` does not validate
    config: an ontology block can be removed/malformed while a proposal is still
    pending.)"""

    def __init__(self, project: str, detail: str) -> None:
        super().__init__(
            f"cannot accept into project {project!r}: {detail}. Configure a valid ontology "
            "(non-empty entity_types AND relation_types) before accepting proposals."
        )
        self.project = project
        self.detail = detail


async def list_ontology_proposals(
    conn: AsyncConnection,
    project: str,
    *,
    limit: int,
    after: uuid.UUID | None = None,
    status: str | None = None,
) -> tuple[list[OntologyProposal], uuid.UUID | None]:
    """One page of a project's pool, id desc (the BA3 keyset pattern). The pool
    is NOT build-scoped (a review artifact), so this reads by project directly.
    ``status`` (GOV4-style facet) narrows to one §17 status; None = the default
    review queue (``proposed`` only), so decided rows are the audit surface a
    consumer opts into by naming the status — same discipline as the
    merge-candidate list."""
    op = tables.ontology_proposals
    where = [op.c.project == project]
    where.append(op.c.status == status if status is not None else op.c.status == "proposed")
    if after is not None:
        where.append(op.c.id < after)
    rows = (
        await conn.execute(
            sa.select(*_PROPOSAL_COLS).where(*where).order_by(op.c.id.desc()).limit(limit + 1)
        )
    ).all()
    proposals = [OntologyProposal(*r) for r in rows[:limit]]
    next_after = proposals[-1].id if len(rows) > limit and proposals else None
    return proposals, next_after


async def decide_ontology_proposal(
    conn: AsyncConnection,
    *,
    project: str,
    proposal_id: uuid.UUID,
    verb: str,
    decided_by: str,
    reason: str | None = None,
) -> OntologyProposal:
    """Record a curator's accept/reject on a pooled proposal (§17). On ACCEPT
    the proposed type joins the project's configured ontology — the SAME
    ``projects.config`` the extractor reads next build — so the next build stops
    holding that type out. Locks the projects row FIRST (config serialization,
    the same row a concurrent config PATCH takes) then the proposal, so a
    concurrent accept + config edit can't lose the type. Does NOT commit — the
    caller owns the transaction.

    Raises ``ProjectNotFoundError``, ``OntologyProposalNotFoundError``,
    ``InvalidProposalTransitionError`` (§17 refusal), or ``ValueError`` for a
    bad verb / an ``AUTO_DECIDER`` curator name (§27.3 precedence keys off it).
    """
    if verb not in _PROPOSAL_VERB_TO_STATUS:
        raise ValueError(
            f"unknown decision verb {verb!r} (choose from {sorted(_PROPOSAL_VERB_TO_STATUS)})"
        )
    if decided_by == AUTO_DECIDER or not decided_by:
        raise ValueError(
            f"decided_by {decided_by!r} is reserved/empty — curator entries must not "
            "impersonate the pipeline (§27.3 precedence keys off it)"
        )
    projects = tables.projects
    locked = (
        await conn.execute(
            sa.select(projects.c.name, projects.c.config)
            .where(projects.c.name == project)
            .with_for_update()
        )
    ).one_or_none()
    if locked is None:
        raise ProjectNotFoundError(project)

    op = tables.ontology_proposals
    row = (
        await conn.execute(
            sa.select(*_PROPOSAL_COLS)
            .where(op.c.id == proposal_id, op.c.project == project)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise OntologyProposalNotFoundError(project, proposal_id)
    target = _PROPOSAL_VERB_TO_STATUS[verb]
    if not can_transition("ontology_proposal", row.status, target):
        raise InvalidProposalTransitionError(proposal_id, row.status, verb)

    if verb == "accept":
        # Lazy import: core.builds.config imports THIS module (the cycle is only
        # at module scope). The gate is the BUILD's OWN loader, not a
        # re-implemented predicate that could drift (Codex #97 R1).
        from core.builds.config import BuildConfigError, ensure_ontology_buildable

        config: dict[str, Any] = dict(locked.config) if isinstance(locked.config, dict) else {}

        def _require_buildable(cfg: dict[str, Any]) -> None:
            try:
                ensure_ontology_buildable(cfg)
            except BuildConfigError as exc:
                raise OntologyConfigIncompleteError(project, str(exc)) from exc

        # accept ADDS a type to an EXISTING valid ontology. Validate the CURRENT
        # config FIRST — before any read/normalization — so an incomplete or
        # malformed block is REFUSED, never silently repaired by the append
        # (Codex #97 R2): reading `block.get(list_key) or []` would turn a
        # missing list into `[type]` and a string like "Person" into character
        # labels, adopting a config the build would reject. A config PATCH can
        # leave that state (it does not validate) while a proposal is pending.
        _require_buildable(config)
        # now known-valid: config["ontology"][list_key] is a non-empty list of
        # strings (a missing/blank list would have failed the gate above), so
        # this reads the REAL list — never a normalized fabrication.
        block = dict(config["ontology"])
        list_key = "entity_types" if row.kind == "entity" else "relation_types"
        types = list(block[list_key])
        if row.type_name not in types:  # dedup — accepting a type twice is a no-op add
            types.append(row.type_name)
        block[list_key] = types
        config["ontology"] = block
        # the appended type must keep the ontology buildable — guards a blank
        # type_name the DDL's `type_name <> ''` would still admit.
        _require_buildable(config)
        await conn.execute(
            projects.update().where(projects.c.name == project).values(config=config)
        )

    updated = (
        await conn.execute(
            op.update()
            .where(op.c.id == proposal_id)
            .values(status=target, decided_by=decided_by, decided_at=sa.func.now(), reason=reason)
            .returning(*_PROPOSAL_COLS)
        )
    ).one()
    return OntologyProposal(*updated)
