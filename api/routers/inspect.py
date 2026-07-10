"""Inspection endpoints (BA3a) — documents/chunks over the ACTIVE build.

Read-only views of the SoR through the DR-006 build-scoped repo: every request
resolves the active binding ONCE on the request connection — a missing project
is a 404 FIRST (a real project with no active build is 409 NO_ACTIVE_BUILD,
never a lookup miss) — constructs the repo bound to it, and stamps the
binding's build_id into meta (§15's "which build served this"). Keyset
pagination mirrors BA1b's opaque-cursor pattern: documents by (id desc) — the
table carries no created_at, and id is the stable unique keyset; recency
ordering can land additively with the Sort param — chunks by
(document_id asc, ordinal asc), the reading order, total under
UNIQUE(document_id, ordinal).

GAP (DR-002 / owner decision pending, the registry_errors precedent): the
frozen ErrorCode enum has no resource-not-found code for inspect resources
(only PROJECT/BUILD/JOB_NOT_FOUND — mislabeling a missing document as any of
those would mislead a client dispatching on error.code). A miss raises the
framework's 404, which BA0's handler wraps in the envelope with the true 404
status and the documented coarse 4xx code (VALIDATION_ERROR) until a
dedicated code lands.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request

from api.deps import Conn, response_meta
from api.envelope import success
from api.pagination import decode_chunk_cursor, decode_id_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query
from api.schemas import chunk_dto, document_dto, entity_dto, relation_dto, relation_evidence_dto
from core.registry import ProjectNotFoundError, get_project
from core.stores import tables
from core.stores.repo import ActiveBinding, BuildScopedRepo, NoActiveBuildError
from core.stores.repo import resolve_active_binding as _resolve_active_binding

router = APIRouter(tags=["inspect"])


async def _bind(conn: Any, project: str) -> ActiveBinding:
    """Project 404 first, then the DR-001 active resolution (409 if none)."""
    try:
        if await get_project(conn, project) is None:
            raise ProjectNotFoundError(project)
        return await _resolve_active_binding(conn, project)
    except (ProjectNotFoundError, NoActiveBuildError) as exc:
        raise translate_registry_error(exc) from exc


def _not_found(resource: str, resource_id: uuid.UUID) -> HTTPException:
    # see the module docstring GAP note: true 404 status, coarse frozen code
    return HTTPException(status_code=404, detail=f"{resource} {resource_id} not found")


@router.get("/projects/{project}/documents")
async def list_documents_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    docs = tables.documents
    where = []
    if cursor:
        (after_id,) = decode_id_cursor(cursor)
        where.append(docs.c.id < after_id)
    rows = await repo.fetch_page(docs, *where, order_by=[docs.c.id.desc()], limit=limit + 1)
    page = rows[:limit]
    next_cursor = encode_cursor((page[-1].id,)) if len(rows) > limit else None
    return success(
        [document_dto(r) for r in page],
        **response_meta(request),
        build_id=binding.build_id,
        paginated=True,
        next_cursor=next_cursor,
    )


@router.get("/projects/{project}/documents/{document_id}")
async def get_document_endpoint(
    request: Request, conn: Conn, project: str, document_id: uuid.UUID
) -> dict[str, Any]:
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rows = await repo.fetch_all(tables.documents, tables.documents.c.id == document_id)
    if not rows:
        raise _not_found("document", document_id)
    return success(
        document_dto(rows[0], include_raw=True),
        **response_meta(request),
        build_id=binding.build_id,
    )


@router.get("/projects/{project}/chunks")
async def list_chunks_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    # the default order is compound — no explicit sort can restate it
    reject_unsupported_query(request, None)
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    chunks = tables.chunks
    where = []
    if cursor:
        after_doc, after_ordinal = decode_chunk_cursor(cursor)
        where.append(sa.tuple_(chunks.c.document_id, chunks.c.ordinal) > (after_doc, after_ordinal))
    rows = await repo.fetch_page(
        chunks,
        *where,
        order_by=[chunks.c.document_id.asc(), chunks.c.ordinal.asc()],
        limit=limit + 1,
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor((page[-1].document_id, page[-1].ordinal)) if len(rows) > limit else None
    )
    return success(
        [chunk_dto(r) for r in page],
        **response_meta(request),
        build_id=binding.build_id,
        paginated=True,
        next_cursor=next_cursor,
    )


@router.get("/projects/{project}/chunks/{chunk_id}")
async def get_chunk_endpoint(
    request: Request, conn: Conn, project: str, chunk_id: uuid.UUID
) -> dict[str, Any]:
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rows = await repo.fetch_all(tables.chunks, tables.chunks.c.id == chunk_id)
    if not rows:
        raise _not_found("chunk", chunk_id)
    return success(chunk_dto(rows[0]), **response_meta(request), build_id=binding.build_id)


@router.get("/projects/{project}/entities")
async def list_entities_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    ents = tables.entities
    where = []
    if cursor:
        (after_id,) = decode_id_cursor(cursor)
        where.append(ents.c.id < after_id)
    rows = await repo.fetch_page(ents, *where, order_by=[ents.c.id.desc()], limit=limit + 1)
    page = rows[:limit]
    next_cursor = encode_cursor((page[-1].id,)) if len(rows) > limit else None
    return success(
        [entity_dto(r) for r in page],
        **response_meta(request),
        build_id=binding.build_id,
        paginated=True,
        next_cursor=next_cursor,
    )


@router.get("/projects/{project}/entities/{entity_id}")
async def get_entity_endpoint(
    request: Request, conn: Conn, project: str, entity_id: uuid.UUID
) -> dict[str, Any]:
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rows = await repo.fetch_all(tables.entities, tables.entities.c.id == entity_id)
    if not rows:
        raise _not_found("entity", entity_id)
    return success(entity_dto(rows[0]), **response_meta(request), build_id=binding.build_id)


@router.get("/projects/{project}/relations")
async def list_relations_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rels = tables.relations
    where = []
    if cursor:
        (after_id,) = decode_id_cursor(cursor)
        where.append(rels.c.id < after_id)
    rows = await repo.fetch_page(rels, *where, order_by=[rels.c.id.desc()], limit=limit + 1)
    page = rows[:limit]
    next_cursor = encode_cursor((page[-1].id,)) if len(rows) > limit else None
    return success(
        [relation_dto(r) for r in page],
        **response_meta(request),
        build_id=binding.build_id,
        paginated=True,
        next_cursor=next_cursor,
    )


@router.get("/projects/{project}/relations/{relation_id}")
async def get_relation_endpoint(
    request: Request, conn: Conn, project: str, relation_id: uuid.UUID
) -> dict[str, Any]:
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rows = await repo.fetch_all(tables.relations, tables.relations.c.id == relation_id)
    if not rows:
        raise _not_found("relation", relation_id)
    # detail carries evidence (the getRelation summary: "with evidence") —
    # scoped fetch, then a deterministic in-Python id order (created_at is a
    # statement timestamp, identical for rows written in one transaction — not
    # a total order; id is unique). No silent cap: a relation's evidence set
    # is §27.4-bounded by the per-source dedup, so reading it whole is honest.
    evidence_rows = await repo.fetch_all(
        tables.relation_evidence, tables.relation_evidence.c.relation_id == relation_id
    )
    evidence = [relation_evidence_dto(e) for e in sorted(evidence_rows, key=lambda e: str(e.id))]
    return success(
        relation_dto(rows[0], evidence=evidence),
        **response_meta(request),
        build_id=binding.build_id,
    )
