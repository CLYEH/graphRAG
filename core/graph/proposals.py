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

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from core.graph.documents import TypeProposal
from core.resolve import fingerprints
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
