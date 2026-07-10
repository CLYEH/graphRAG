"""Request DTOs and response serializers for the registry/job endpoints (BA1b/BA2e).

The request models mirror the frozen contract ProjectCreate/ProjectUpdate/
SourceCreate (min_length on the required strings, so FastAPI rejects empties as
VALIDATION_ERROR before the handler). The serializers project the BA1a
dataclasses onto exactly the contract Project/Source field sets — Source drops
the internal ``project`` (the contract Source does not carry it). Datetimes/
UUIDs stay as objects; the envelope's jsonable_encoder renders them.

IngestRequest/BuildRequest carry fields the pipeline cannot honor yet
(``source_ids`` — core's ingest stage has no source filter; ``reason`` — builds
have no note column). Owner decision (2026-07-10): reject them LOUDLY (400)
rather than accept-and-ignore — running a full ingest against an explicit
restriction, or dropping an operator's note, would silently disobey the request.
Lifting a rejection later is additive (no contract bump).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from core.registry import Job, Project, Source


def _reject_null_config(v: dict[str, Any] | None) -> dict[str, Any] | None:
    # contract: config is `type: object` (non-nullable) in both ProjectCreate
    # and ProjectUpdate — unlike display_name/description, which ARE nullable
    # (they clear the column). An explicit null is a client error (400), not a
    # NOT NULL IntegrityError deep in the registry (500). Omitting the field is
    # how you leave it unchanged; the omitted default never reaches this
    # validator. See the registry's config: dict | _UNSET (never None).
    if v is None:
        raise ValueError("config may not be null; omit it to leave it unchanged")
    return v


#: config that rejects an explicit null but stays optional (omit → default None).
NonNullConfig = Annotated[dict[str, Any] | None, AfterValidator(_reject_null_config)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    display_name: str | None = None
    description: str | None = None
    config: NonNullConfig = None


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    description: str | None = None
    config: NonNullConfig = None


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1)
    kind: str | None = None
    metadata: dict[str, Any] | None = None


class IngestRequest(BaseModel):
    """The contract IngestRequest — parsed to its shape, then ``source_ids``
    is loudly rejected while PRESENT (even as an explicit null — the contract
    types it as a non-nullable array, same strictness as config's null) until
    the pipeline can honor the restriction."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[uuid.UUID] | None = None

    @model_validator(mode="after")
    def _reject_source_ids(self) -> IngestRequest:
        # see the module docstring: no source filter exists in the pipeline
        # yet, so a restricted ingest must fail loud, never silently run
        # unrestricted.
        if "source_ids" in self.model_fields_set:
            raise ValueError(
                "source_ids restriction is not supported yet; omit it to ingest all sources"
            )
        return self


class BuildRequest(BaseModel):
    """The contract BuildRequest — parsed to its shape, then ``reason`` is
    loudly rejected while PRESENT until builds can record it."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None

    @model_validator(mode="after")
    def _reject_reason(self) -> BuildRequest:
        # see the module docstring: builds carry no note column yet, so a
        # reason must fail loud, never be silently dropped.
        if "reason" in self.model_fields_set:
            raise ValueError("reason is not recorded on builds yet; omit it")
        return self


class ReviewDecisionRequest(BaseModel):
    """The contract ReviewDecisionRequest — an optional free-form reason,
    contract-nullable (unlike the BA2e trigger fields, ``reason`` here IS
    honored: it lands on both the ledger entry and the candidate row)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class QueryRequest(BaseModel):
    """The contract QueryRequest. ``filters``/``options`` are parsed to shape
    then LOUDLY rejected while present (the BA2e owner rule, 2026-07-10): the
    §8 modes take no store-level filters or mode options yet — silently
    running an UNfiltered query against an explicit restriction would return
    results the client did not ask for. Lifting is additive."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1)
    filters: dict[str, Any] | None = None
    options: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _reject_unsupported(self) -> QueryRequest:
        for field in ("filters", "options"):
            if field in self.model_fields_set:
                raise ValueError(f"{field} is not supported yet; omit it")
        return self


def project_dto(p: Project) -> dict[str, Any]:
    """The contract Project shape."""
    return {
        "name": p.name,
        "display_name": p.display_name,
        "description": p.description,
        "config": p.config,
        "created_at": p.created_at,
    }


def source_dto(s: Source) -> dict[str, Any]:
    """The contract Source shape (no ``project`` — it is path context)."""
    return {
        "id": s.id,
        "kind": s.kind,
        "uri": s.uri,
        "metadata": s.metadata,
        "added_at": s.added_at,
    }


def job_dto(j: Job) -> dict[str, Any]:
    """The contract Job shape, FULL and always present (nullable fields are
    null, never absent — §27.2's no-branching-on-missing-fields doctrine).
    ``id`` becomes the contract's ``job_id``; the internal cancel_requested /
    lease / config_snapshot fields are not part of the frozen shape."""
    return {
        "job_id": j.id,
        "status": j.status,
        "kind": j.kind,
        "project": j.project,
        "build_id": j.build_id,
        "step": j.step,
        "progress": j.progress,
        "message": j.message,
        "error": j.error,
        "created_at": j.created_at,
        "finished_at": j.finished_at,
    }


def job_accepted_dto(j: Job) -> dict[str, Any]:
    """The contract JobAccepted shape — the 202 payload for long operations."""
    return {"job_id": j.id, "status": j.status}


def document_dto(row: Any, *, include_raw: bool = False) -> dict[str, Any]:
    """The contract Document shape from a scoped ``documents`` row. Two kinds
    of conditional key, each the only legal encoding: ``raw`` is
    contract-licensed detail-only ("returned on detail GET only"); ``status``
    is an OPTIONAL NON-NULLABLE string in the frozen schema while the column
    is nullable — a NULL column can only be expressed by OMITTING the key
    (emitting null would be contract-invalid). Nullable-typed fields
    (mime/ingested_at) stay always-present; metadata coalesces DB NULL to {}
    (the contract types it as a non-nullable object, and 'no metadata' IS the
    empty object)."""
    dto = {
        "id": row.id,
        "project": row.project,
        "build_id": row.build_id,
        "source_uri": row.source_uri,
        "content_hash": row.content_hash,
        "mime": row.mime,
        "metadata": row.metadata or {},
        "ingested_at": row.ingested_at,
    }
    if row.status is not None:
        dto["status"] = row.status
    if include_raw:
        dto["raw"] = row.raw
    return dto


def chunk_dto(row: Any) -> dict[str, Any]:
    """The contract Chunk shape from a scoped ``chunks`` row. ``status`` is
    optional NON-nullable in the frozen schema and the cleaning path writes
    chunks without one — a NULL column is expressed by omitting the key
    (see document_dto)."""
    dto = {
        "id": row.id,
        "document_id": row.document_id,
        "build_id": row.build_id,
        "ordinal": row.ordinal,
        "text": row.text,
        "token_count": row.token_count,
        "start_offset": row.start_offset,
        "end_offset": row.end_offset,
        "vector_point_id": row.vector_point_id,
        "metadata": row.metadata or {},
    }
    if row.status is not None:
        dto["status"] = row.status
    return dto


def merge_candidate_dto(row: Any) -> dict[str, Any]:
    """The contract MergeCandidate shape from a scoped row (works for both a
    ``merge_candidates`` Row and core's MergeCandidate dataclass — same
    attribute names). Per-field audit (#55 rule): required columns NOT NULL;
    ``features`` is an optional NON-nullable object over a nullable column →
    {} for NULL; ``decision``/``decided_by``/``decided_at``/``reason`` and
    ``impact``/``left_snapshot``/``right_snapshot`` are contract-NULLABLE →
    emitted as-is (null is legal for them, unlike features)."""
    return {
        "id": row.id,
        "project": row.project,
        "build_id": row.build_id,
        "left_entity_id": row.left_entity_id,
        "right_entity_id": row.right_entity_id,
        "score": row.score,
        "features": row.features or {},
        "status": row.status,
        "decision": row.decision,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "reason": row.reason,
        "impact": row.impact,
        "left_snapshot": row.left_snapshot,
        "right_snapshot": row.right_snapshot,
    }


def entity_dto(row: Any) -> dict[str, Any]:
    """The contract Entity shape from a scoped ``entities`` row. Per-field
    nullability audit (the #55 rule): every required field's column is NOT
    NULL; ``created_by`` is an optional NON-nullable enum over a nullable
    column → omit-when-null; ``attributes`` coalesces DB NULL to {} (optional
    non-nullable object); created_at/updated_at are contract-nullable.
    ``embedding_point_id`` is internal (not a contract property) and never
    emitted."""
    dto = {
        "id": row.id,
        "project": row.project,
        "build_id": row.build_id,
        "type": row.type,
        "canonical_name": row.canonical_name,
        "entity_key": row.entity_key,
        "attributes": row.attributes or {},
        "status": row.status,
        "review_status": row.review_status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if row.created_by is not None:
        dto["created_by"] = row.created_by
    return dto


def relation_evidence_dto(row: Any) -> dict[str, Any]:
    """The contract RelationEvidence shape — every optional field is
    contract-nullable (and ``evidence_ref`` is NOT NULL at the column), so the
    full shape is always present. ``relation_id``/``build_id`` (parent
    context) and ``evidence_hash`` (the internal §27.4 dedup key) are not
    contract properties and never emitted."""
    return {
        "id": row.id,
        "evidence_type": row.evidence_type,
        "evidence_ref": row.evidence_ref,
        "chunk_id": row.chunk_id,
        "start_offset": row.start_offset,
        "end_offset": row.end_offset,
        "quote": row.quote,
        "source_uri": row.source_uri,
        "confidence": row.confidence,
    }


def relation_dto(row: Any, *, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The contract Relation shape from a scoped ``relations`` row. Same
    nullability audit as entity_dto: ``created_by`` AND ``relation_signature``
    (legitimately NULL pre-resolve) are optional NON-nullable over nullable
    columns → omit-when-null; ``attributes`` → {}; confidence and the
    timestamps are contract-nullable. ``evidence`` is detail-only (the
    getRelation summary is "Get a relation WITH EVIDENCE"; lists omit the
    optional key rather than fetch N sub-resources per row)."""
    dto = {
        "id": row.id,
        "project": row.project,
        "build_id": row.build_id,
        "src_entity_id": row.src_entity_id,
        "dst_entity_id": row.dst_entity_id,
        "type": row.type,
        "attributes": row.attributes or {},
        "status": row.status,
        "review_status": row.review_status,
        "confidence": row.confidence,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if row.created_by is not None:
        dto["created_by"] = row.created_by
    if row.relation_signature is not None:
        dto["relation_signature"] = row.relation_signature
    if evidence is not None:
        dto["evidence"] = evidence
    return dto


def job_event_dto(j: Job, ts: datetime) -> dict[str, Any]:
    """The contract JobEvent shape — an SSE ``data:`` payload, FULL and always
    present (step/message null, never absent — §27.2's no-branching-on-missing-
    fields doctrine). Distinct from ``job_dto`` (no kind/project/error; adds
    ``ts`` — the DB clock at the moment the state was observed, see
    ``get_job_at``)."""
    return {
        "job_id": j.id,
        "status": j.status,
        "step": j.step,
        "progress": j.progress,
        "message": j.message,
        "ts": ts,
    }
