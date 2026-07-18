"""Inspection endpoints (BA3) — documents/chunks/entities/relations/subgraph
over the ACTIVE build.

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
from dataclasses import replace
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from api.deps import Conn, Graph, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.pagination import decode_chunk_cursor, decode_id_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query, single_filter_value
from api.schemas import chunk_dto, document_dto, entity_dto, relation_dto, relation_evidence_dto
from core.mcp.policy import PolicyError, query_policy_from_mapping
from core.query.graph import subgraph_context
from core.registry import ProjectNotFoundError, get_project
from core.stores import tables
from core.stores.graph import BuildScopedGraphRepo
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


#: SS1a facet vocabularies — the SAME value sets the DDL CHECK constraints
#: enforce (core/stores/tables.py entities/relations_status_valid and
#: *_review_status_valid); a contract test parses the DDL and pins parity,
#: so the filter can never accept a value the column cannot hold (or refuse
#: one it can). Entity/relation TYPE is deliberately open (ontology-defined
#: per project) — only blankness is invalid there.
LIFECYCLE_STATUS: tuple[str, ...] = ("active", "deprecated", "merged", "rejected", "needs_review")
REVIEW_STATUS: tuple[str, ...] = ("unreviewed", "approved", "rejected")


def _escape_like(value: str) -> str:
    """Escape a user search term for a LIKE/ILIKE pattern (SS1b). ``%``/``_``
    are SQL wildcards and ``\\`` the default escape char — without escaping,
    a literal ``%`` a user typed would match anything (a surprising superset)
    and ``\\`` could form an escape sequence. Backslash first so the escapes we
    add are not themselves re-escaped."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/projects/{project}/documents")
async def list_documents_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    q: str | None = Query(None, min_length=1, max_length=256),
) -> dict[str, Any]:
    reject_unsupported_query(request, "id", allowed_filters=frozenset({"status"}), search=True)
    # documents.status is an OPEN vocabulary (no DDL CHECK — the ingest
    # pipeline owns it), so the facet validates blankness only
    status = single_filter_value(request, "status")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    docs = tables.documents
    # SS1b: `q` searches source_uri (the document's visible identifier); content
    # search is the deferred metadata-indexing follow-up. Applied to BOTH the
    # page and the count so total matches the filtered set (a search restricts
    # the row set, so total must reflect it, not the unfiltered table).
    filters = []
    if status is not None:
        filters.append(docs.c.status == status)
    if q is not None:
        filters.append(docs.c.source_uri.ilike(f"%{_escape_like(q)}%", escape="\\"))
    total = await repo.fetch_count(docs, *filters)
    where = [*filters]
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
        total=total,
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
    q: str | None = Query(None, min_length=1, max_length=256),
) -> dict[str, Any]:
    reject_unsupported_query(
        request, "id", allowed_filters=frozenset({"type", "status", "review_status"}), search=True
    )
    etype = single_filter_value(request, "type")
    status = single_filter_value(request, "status", vocabulary=LIFECYCLE_STATUS)
    review_status = single_filter_value(request, "review_status", vocabulary=REVIEW_STATUS)
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    ents = tables.entities
    # SS1b: `q` searches canonical_name (substring, case-insensitive). Applied to
    # both the count and the page so total reflects the searched set.
    filters = []
    if etype is not None:
        filters.append(ents.c.type == etype)
    if status is not None:
        filters.append(ents.c.status == status)
    if review_status is not None:
        filters.append(ents.c.review_status == review_status)
    if q is not None:
        filters.append(ents.c.canonical_name.ilike(f"%{_escape_like(q)}%", escape="\\"))
    total = await repo.fetch_count(ents, *filters)
    where = [*filters]
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
        total=total,
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
    reject_unsupported_query(
        request, "id", allowed_filters=frozenset({"type", "status", "review_status"})
    )
    rtype = single_filter_value(request, "type")
    status = single_filter_value(request, "status", vocabulary=LIFECYCLE_STATUS)
    review_status = single_filter_value(request, "review_status", vocabulary=REVIEW_STATUS)
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    rels = tables.relations
    where = []
    if rtype is not None:
        where.append(rels.c.type == rtype)
    if status is not None:
        where.append(rels.c.status == status)
    if review_status is not None:
        where.append(rels.c.review_status == review_status)
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


@router.get("/projects/{project}/graph/subgraph")
async def get_subgraph_endpoint(
    request: Request,
    conn: Conn,
    driver: Graph,
    project: str,
    entity_id: uuid.UUID,
    hops: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """§21-governed subgraph around one entity, over the ACTIVE build.

    The policy is read from the project's REGISTRY config (owner decision
    2026-07-10: ``projects.config["query_policy"]``, validated against the
    SAME frozen schema as the MCP/CLI file loader — strict, no invented §21
    defaults: an unconfigured project is told so, not silently capped). The
    client's ``limit`` narrows the §21 row ceiling (min, never widens); hops
    beyond ``max_graph_hops`` are rejected, not clamped (the C6c doctrine).
    """
    proj = await get_project(conn, project)
    if proj is None:
        raise translate_registry_error(ProjectNotFoundError(project))
    # bind BEFORE the policy checks: this endpoint serves the ACTIVE build,
    # and a project without one must answer the surface-consistent 409
    # NO_ACTIVE_BUILD — not a 400 telling the client to fix policy when there
    # is no graph to inspect (Codex #57 R1; same precedence as _bind)
    try:
        binding = await _resolve_active_binding(conn, project)
    except NoActiveBuildError as exc:
        raise translate_registry_error(exc) from exc
    block = (proj.config or {}).get("query_policy")
    if block is None:
        # GAP-adjacent (registry_errors precedent): no frozen code says
        # "project not configured for this feature" — VALIDATION_ERROR with a
        # machine-readable detail is the documented coarse mapping
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"project {project!r} has no query_policy configured — "
            "PATCH the project config with a query_policy block (§21)",
            details={"query_policy": "missing"},
        )
    try:
        qp = query_policy_from_mapping(block)
    except PolicyError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, str(exc), details={"query_policy": "invalid"}
        ) from exc
    if hops > qp.max_graph_hops:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"hops={hops} is outside the policy ceiling 1..{qp.max_graph_hops} "
            "(§21 max_graph_hops) — rejected, not clamped",
            details={"hops": hops, "max_graph_hops": qp.max_graph_hops},
        )
    repo = BuildScopedRepo.bound_to(conn, binding)
    cypher = qp.cypher_policy()
    effective = replace(cypher, max_rows=min(limit, cypher.max_rows))
    try:
        async with driver.session() as session:
            graph_repo = BuildScopedGraphRepo.bound_to(session, binding)
            context = await subgraph_context(
                graph_repo,
                repo,
                effective,
                entity_id,
                hops,
                max_graph_hops=qp.max_graph_hops,
            )
    except (Neo4jError, ServiceUnavailable) as exc:
        # the graph projection is a derived STORE — its outage is 503, never a
        # silent empty subgraph (an outage is not an answer) nor a 500
        raise ApiError(
            ErrorCode.STORE_UNAVAILABLE,
            "graph store unavailable or failed while building the subgraph",
        ) from exc
    if context is None:
        raise _not_found("entity", entity_id)
    return success(
        {"nodes": list(context.nodes), "edges": list(context.edges)},
        **response_meta(request),
        build_id=binding.build_id,
    )
