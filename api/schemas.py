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
