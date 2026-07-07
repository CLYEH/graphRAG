"""Request DTOs and response serializers for the registry endpoints (BA1b).

The request models mirror the frozen contract ProjectCreate/ProjectUpdate/
SourceCreate (min_length on the required strings, so FastAPI rejects empties as
VALIDATION_ERROR before the handler). The serializers project the BA1a
dataclasses onto exactly the contract Project/Source field sets — Source drops
the internal ``project`` (the contract Source does not carry it). Datetimes/
UUIDs stay as objects; the envelope's jsonable_encoder renders them.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from core.registry import Project, Source


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
