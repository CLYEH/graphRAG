"""Semantic retrieval: Qdrant kNN → §16 response (DESIGN §8/§16/§27.2, C6a).

The first retrieval modality (§8 semantic): embed the query with the SAME
abstraction the index step used (§3), run a build-scoped kNN over Qdrant, and
turn each hit into a §16 result that CITES ITS SOURCE. The scope is not
re-derived here — the caller passes a ``BuildScopedVectorRepo`` and a
``BuildScopedRepo`` already bound to the active build (DR-001), and both are
checked to agree so enrichment can never read one build's Postgres rows to
back another build's vector hits.

Why enrichment at all: Qdrant holds only the payload the index step stamped
(``{canonical_id, type, text, chunk_id|entity_id, ...}``) — enough to identify
the hit, not enough to CITE it. §27.2 require_sources demands a chunk result
carry ``source_uri + offsets`` and an entity result carry ≥1 mention (chunk or
row). Those live in Postgres, so each hit is enriched from the SoR through the
same build-scoped repo. A hit that cannot be enriched to a contract-valid,
citable result (its chunk/document row or all its mentions are gone — projection
drift, §19) is DROPPED, not emitted uncited, and the drop is surfaced as a
typed ``PARTIAL_RESULTS`` warning (§22) rather than silently swallowed.

Ordering and the whole envelope shape are inherited from
:mod:`core.query.results` (score desc, ties by id; ``graph_context``/``debug``
null — semantic is single-mode, no router trace, and debug gating +
latency live at the C8 tool boundary).
"""

from __future__ import annotations

import uuid
from typing import Any

from llama_index.core.embeddings import BaseEmbedding
from qdrant_client import models

from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)
from core.stores import tables
from core.stores.repo import BuildScopedRepo
from core.stores.vectors import BuildScopedVectorRepo

_TOOL = "semantic_search"

#: §27.2 entity source_ref: a mention's ``source_kind`` decides which citable
#: source_type it is (a ``text`` mention came from a chunk, a ``structured``
#: one from a table row). The two values are the frozen entity_mentions CHECK
#: vocabulary, so this map is total.
_MENTION_SOURCE_TYPE = {"text": "chunk", "structured": "row"}


async def semantic_search(
    repo: BuildScopedRepo,
    vectors: BuildScopedVectorRepo,
    embedder: BaseEmbedding,
    query: str,
    top_k: int,
) -> McpResponse:
    """§8 semantic kNN over the active build, as a §16 response.

    ``repo`` and ``vectors`` must be bound to the same active
    ``(project, build_id)`` — the caller resolves the active build once and
    mints both (DR-001). ``embedder`` must be the model the index step used, so
    query and stored vectors share a space (§3); only
    :meth:`aget_text_embedding` is used.
    """
    if (repo.project, repo.build_id) != (vectors.project, vectors.build_id):
        raise ValueError(
            "repo and vector-repo scopes disagree "
            f"({repo.project}/{repo.build_id} vs {vectors.project}/{vectors.build_id}) — "
            "both must bind the same active build or enrichment would cross scopes"
        )

    query_vector = await embedder.aget_text_embedding(query)
    hits = await vectors.search(query_vector, limit=top_k)

    chunk_ids, entity_ids = _partition_hit_source_ids(hits)
    chunk_by_id, source_uri_by_chunk = await _load_chunk_provenance(repo, chunk_ids)
    mentions_by_entity = await repo.mentions_by_entity(list(entity_ids))

    results: list[RetrievalResult] = []
    dropped = 0
    for hit in hits:
        payload = hit.payload
        if payload is None:
            dropped += 1
            continue
        result = _build_result(
            payload, hit.score, chunk_by_id, source_uri_by_chunk, mentions_by_entity
        )
        if result is None:
            dropped += 1
            continue
        results.append(result)

    warnings: tuple[QueryWarning, ...] = ()
    if dropped:
        warnings = (
            QueryWarning(
                "PARTIAL_RESULTS",
                f"{dropped} hit(s) omitted: no citable source in the active build "
                "(projection drift — see Health)",
            ),
        )

    return McpResponse(
        query=query,
        tool=_TOOL,
        project=repo.project,
        build_id=str(repo.build_id),
        results=ordered_results(results),
        warnings=warnings,
    )


def _partition_hit_source_ids(
    hits: list[models.ScoredPoint],
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Collect the source row ids to enrich, split by point type (§4 payload)."""
    chunk_ids: set[uuid.UUID] = set()
    entity_ids: set[uuid.UUID] = set()
    for hit in hits:
        payload = hit.payload
        if payload is None:
            continue
        if payload.get("type") == "chunk" and payload.get("chunk_id"):
            chunk_ids.add(uuid.UUID(payload["chunk_id"]))
        elif payload.get("type") == "entity" and payload.get("entity_id"):
            entity_ids.add(uuid.UUID(payload["entity_id"]))
    return chunk_ids, entity_ids


async def _load_chunk_provenance(
    repo: BuildScopedRepo, chunk_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, Any], dict[uuid.UUID, str | None]]:
    """The chunk rows (offsets) + their documents' source_uri, in two bulk reads
    (build-scoped) so enrichment is not N+1 over the hits."""
    if not chunk_ids:
        return {}, {}
    chunk_rows = await repo.fetch_all(tables.chunks, tables.chunks.c.id.in_(chunk_ids))
    chunk_by_id = {row.id: row for row in chunk_rows}
    document_ids = {row.document_id for row in chunk_rows}
    uri_by_document: dict[uuid.UUID, str | None] = {}
    if document_ids:
        document_rows = await repo.fetch_all(
            tables.documents, tables.documents.c.id.in_(document_ids)
        )
        uri_by_document = {row.id: row.source_uri for row in document_rows}
    source_uri_by_chunk = {row.id: uri_by_document.get(row.document_id) for row in chunk_rows}
    return chunk_by_id, source_uri_by_chunk


def _build_result(
    payload: dict[str, Any],
    score: float,
    chunk_by_id: dict[uuid.UUID, Any],
    source_uri_by_chunk: dict[uuid.UUID, str | None],
    mentions_by_entity: dict[uuid.UUID, list[tuple[str, str]]],
) -> RetrievalResult | None:
    """One hit → one §16 result, or None if it cannot be cited (drop it)."""
    point_type = payload.get("type")
    if point_type == "chunk":
        return _chunk_result(payload, score, chunk_by_id, source_uri_by_chunk)
    if point_type == "entity":
        return _entity_result(payload, score, mentions_by_entity)
    return None  # an unknown point type cannot be mapped to a §16 result_type


def _chunk_result(
    payload: dict[str, Any],
    score: float,
    chunk_by_id: dict[uuid.UUID, Any],
    source_uri_by_chunk: dict[uuid.UUID, str | None],
) -> RetrievalResult | None:
    raw = payload.get("chunk_id")
    if not raw:
        return None
    chunk_id = uuid.UUID(raw)
    chunk = chunk_by_id.get(chunk_id)
    source_uri = source_uri_by_chunk.get(chunk_id)
    if chunk is None or not source_uri:
        return None  # §27.2 chunk ref needs source_uri + offsets; drift lost the row
    ref = SourceRef(
        source_type="chunk",
        id=str(chunk_id),
        source_uri=source_uri,
        metadata={"start_offset": chunk.start_offset, "end_offset": chunk.end_offset},
    )
    return RetrievalResult(
        result_type="chunk",
        id=str(chunk_id),
        score=score,
        source_refs=(ref,),
        text=payload.get("text"),
    )


def _entity_result(
    payload: dict[str, Any],
    score: float,
    mentions_by_entity: dict[uuid.UUID, list[tuple[str, str]]],
) -> RetrievalResult | None:
    raw = payload.get("entity_id")
    if not raw:
        return None
    entity_id = uuid.UUID(raw)
    refs = tuple(
        SourceRef(source_type=source_type, id=source_ref)
        for kind, source_ref in mentions_by_entity.get(entity_id, [])
        if (source_type := _MENTION_SOURCE_TYPE.get(kind)) is not None
    )
    if not refs:
        return None  # §27.2 entity ref needs ≥1 chunk/row mention; none survived
    return RetrievalResult(
        result_type="entity",
        id=payload.get("canonical_id") or str(entity_id),
        score=score,
        source_refs=refs,
        title=payload.get("text"),
    )
