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
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from api.deps import Conn, Graph, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.pagination import (
    decode_chunk_cursor,
    decode_id_cursor,
    decode_sorted_cursor,
    encode_cursor,
    encode_sorted_cursor,
)
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query, single_filter_value
from api.schemas import chunk_dto, document_dto, entity_dto, relation_dto, relation_evidence_dto
from core.mcp.policy import PolicyError, query_policy_from_mapping
from core.metadata.schema import filterable_attributes
from core.observability.health import LOW_CONFIDENCE_BELOW
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

#: GOV2-facet quality facets on /relations (owner-ratified D4). CLOSED single-value
#: vocabularies — not free-form numbers — so the predicate lives server-side in ONE
#: place: `confidence=low` means `confidence < LOW_CONFIDENCE_BELOW` (the SAME §19
#: constant health.py's low_confidence_relations gauge uses; NULL confidence is NOT
#: low — SQL three-valued logic, mirroring the gauge) and `evidence=missing` means
#: no relation_evidence row exists (the gauge's NOT EXISTS). Anything else 400s.
CONFIDENCE_FACETS: tuple[str, ...] = ("low",)
EVIDENCE_FACETS: tuple[str, ...] = ("missing",)


def _escape_like(value: str) -> str:
    """Escape a user search term for a LIKE/ILIKE pattern (SS1b). ``%``/``_``
    are SQL wildcards and ``\\`` the default escape char — without escaping,
    a literal ``%`` a user typed would match anything (a surprising superset)
    and ``\\`` could form an escape sequence. Backslash first so the escapes we
    add are not themselves re-escaped."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: COALESCE floor for the NULLABLE timestamp sorts (SS1b): NULL rows order as
#: the epoch — deterministically OLDEST, so they land last under desc and
#: first under asc; the cursor carries the coalesced value, keeping the
#: keyset tuple total without a NOT NULL migration.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _sorted_page(
    sort: str | None,
    cursor: str | None,
    id_col: Any,
    where: list[Any],
    *,
    text_cols: dict[str, Any],
    time_cols: dict[str, Any],
) -> tuple[list[Any], Any]:
    """ORDER BY + next-cursor minting for the SS1b sort allowlist.

    ``sort`` arrives ALREADY validated by ``reject_unsupported_query``
    (``extra_sorts``), so an unknown field here would be a wiring bug — it
    raises KeyError loudly rather than falling back to a wrong order. The
    default (``sort is None``) keeps the legacy untagged ``(id desc)``
    cursor shape so in-flight pre-sort cursors stay valid; every sorted
    order uses a TAGGED cursor bound to its sort spelling
    (``decode_sorted_cursor`` rejects cross-sort replays). Non-unique sort
    columns tie-break on ``id`` in the same direction, making every keyset
    total."""
    if sort == f"{id_col.name}:desc":
        sort = None  # an explicit restatement of the default IS the default
    if sort is None:
        if cursor:
            (after_id,) = decode_id_cursor(cursor)
            where.append(id_col < after_id)

        def mint_default(last: Any) -> str:
            return encode_cursor((last.id,))

        return [id_col.desc()], mint_default

    field, _, direction = sort.partition(":")
    ascending = direction == "asc"
    if field in text_cols:
        col = text_cols[field]
        expr = col
        types: tuple[type, ...] = (str, uuid.UUID)

        def values_of(last: Any) -> tuple[Any, ...]:
            return (getattr(last, col.name), last.id)

    else:
        col = time_cols[field]
        expr = sa.func.coalesce(col, _EPOCH)
        types = (datetime, uuid.UUID)

        def values_of(last: Any) -> tuple[Any, ...]:
            return (getattr(last, col.name) or _EPOCH, last.id)

    if cursor:
        after = decode_sorted_cursor(cursor, sort, types)
        keyset = sa.tuple_(expr, id_col)
        where.append(keyset > after if ascending else keyset < after)
    order = [expr.asc(), id_col.asc()] if ascending else [expr.desc(), id_col.desc()]

    def mint(last: Any) -> str:
        return encode_sorted_cursor(sort, values_of(last))

    return order, mint


def _typed_metadata_value(raw: str, declared: str, name: str) -> str | float | int | bool:
    """Cast a ``filter[<attr>]`` string to the attribute's DECLARED type for
    JSONB containment — matching by the schema's type, not by guessing from
    the spelling. Booleans are STRICT true/false (class 1: a "True"/"1" is a
    caller typo, not a match-nothing predicate); numbers parse int-first so
    the containment probe compares numerically either way."""
    if declared == "string":
        return raw
    if declared == "number":
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError as exc:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    f"filter[{name}] expects a number (schema-declared type)",
                    details={f"filter[{name}]": raw},
                ) from exc
    if declared == "boolean":
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"filter[{name}] expects true or false (schema-declared type)",
            details={f"filter[{name}]": raw},
        )
    raise ApiError(  # unreachable: loader vocabulary-checks declared types
        ErrorCode.VALIDATION_ERROR, f"unsupported declared type {declared!r} for filter[{name}]"
    )


@router.get("/projects/{project}/documents")
async def list_documents_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    q: str | None = Query(None, min_length=1, max_length=256),
) -> dict[str, Any]:
    # the project row is fetched for its config: metadata_schema's filterable
    # attributes become live filter[<attr>] facets (DR-010 rule 2 / review
    # rule 8's SEARCH half, SS1b) — the allowlist is the schema itself, so a
    # project that declares nothing filterable accepts nothing extra
    project_row = await get_project(conn, project)
    if project_row is None:
        raise translate_registry_error(ProjectNotFoundError(project))
    fattrs = filterable_attributes(dict(project_row.config or {}))
    # reserved: the lifecycle facet owns filter[status] — a filterable attr of
    # that name is unreachable here (declare a different name); silently
    # letting it shadow the lifecycle facet would flip the meaning of an
    # existing spelling
    fattrs.pop("status", None)
    docs = tables.documents
    reject_unsupported_query(
        request,
        "id",
        allowed_filters=frozenset({"status"} | fattrs.keys()),
        search=True,
        # SS1b sort expansion: recency (nullable — COALESCE keyset)
        extra_sorts=frozenset({"ingested_at"}),
    )
    # documents.status is an OPEN vocabulary (no DDL CHECK — the ingest
    # pipeline owns it), so the facet validates blankness only
    status = single_filter_value(request, "status")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    # SS1b: `q` searches source_uri (the document's visible identifier); content
    # search is the deferred metadata-indexing follow-up. Applied to BOTH the
    # page and the count so total matches the filtered set (a search restricts
    # the row set, so total must reflect it, not the unfiltered table).
    filters = []
    if status is not None:
        filters.append(docs.c.status == status)
    if q is not None:
        filters.append(docs.c.source_uri.ilike(f"%{_escape_like(q)}%", escape="\\"))
    for attr_name, declared in sorted(fattrs.items()):
        raw = single_filter_value(request, attr_name)
        if raw is None:
            continue
        value = _typed_metadata_value(raw, declared, attr_name)
        # JSONB containment down the envelope path — served by the GIN
        # jsonb_path_ops index (migration 0020); equality is by JSONB value,
        # so int-vs-float spellings of one number still match
        filters.append(docs.c.metadata.contains({"context": {"attributes": {attr_name: value}}}))
    total = await repo.fetch_count(docs, *filters)
    where = [*filters]
    order_by, mint_cursor = _sorted_page(
        request.query_params.get("sort"),
        cursor,
        docs.c.id,
        where,
        text_cols={},
        time_cols={"ingested_at": docs.c.ingested_at},
    )
    rows = await repo.fetch_page(docs, *where, order_by=order_by, limit=limit + 1)
    page = rows[:limit]
    next_cursor = mint_cursor(page[-1]) if len(rows) > limit else None
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
    ents = tables.entities
    reject_unsupported_query(
        request,
        "id",
        allowed_filters=frozenset({"type", "status", "review_status"}),
        search=True,
        # SS1b sort expansion (owner-approved minimal set): the display name
        # (NOT NULL, tie-broken on id) and recency (nullable — COALESCE keyset)
        extra_sorts=frozenset({"canonical_name", "created_at"}),
    )
    etype = single_filter_value(request, "type")
    status = single_filter_value(request, "status", vocabulary=LIFECYCLE_STATUS)
    review_status = single_filter_value(request, "review_status", vocabulary=REVIEW_STATUS)
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
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
    order_by, mint_cursor = _sorted_page(
        request.query_params.get("sort"),
        cursor,
        ents.c.id,
        where,
        text_cols={"canonical_name": ents.c.canonical_name},
        time_cols={"created_at": ents.c.created_at},
    )
    rows = await repo.fetch_page(ents, *where, order_by=order_by, limit=limit + 1)
    page = rows[:limit]
    next_cursor = mint_cursor(page[-1]) if len(rows) > limit else None
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
        request,
        "id",
        allowed_filters=frozenset({"type", "status", "review_status", "confidence", "evidence"}),
    )
    rtype = single_filter_value(request, "type")
    status = single_filter_value(request, "status", vocabulary=LIFECYCLE_STATUS)
    review_status = single_filter_value(request, "review_status", vocabulary=REVIEW_STATUS)
    confidence = single_filter_value(request, "confidence", vocabulary=CONFIDENCE_FACETS)
    evidence = single_filter_value(request, "evidence", vocabulary=EVIDENCE_FACETS)
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
    if confidence is not None:
        # `low` — the same strict-< predicate as the §19 gauge (NULL is not low)
        where.append(rels.c.confidence < LOW_CONFIDENCE_BELOW)
    if evidence is not None:
        # `missing` — the gauge's NOT EXISTS over relation_evidence
        where.append(
            ~sa.exists(
                sa.select(sa.literal(1)).where(tables.relation_evidence.c.relation_id == rels.c.id)
            )
        )
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
