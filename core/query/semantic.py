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
citable result — its chunk/document row or all its mentions are gone
(projection drift, §19), its point type is one this tool doesn't map, or its
source id is corrupt (a non-UUID payload) — is DROPPED, not emitted uncited and
not allowed to raise, and the drop is surfaced as a typed ``PARTIAL_RESULTS``
warning (§22 degradation-not-failure) rather than silently swallowed.

Every payload value that reaches the response is treated as untrusted (a
projection can drift/corrupt): IDENTIFYING fields (result id, source ref ids)
are derived from the VALIDATED UUID (never the raw ``canonical_id`` string) and
Postgres columns, and OPTIONAL display fields (``text``/``title``) are coerced
to None if non-string — so one corrupt hit can never make the whole §16
response schema-invalid (see :func:`_payload_uuid` / :func:`_payload_str`).

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


def _payload_uuid(raw: object) -> uuid.UUID | None:
    """A payload source id as a UUID, or None if absent/blank/malformed.

    The index projector only ever writes ``str(uuid)``, so in a healthy build
    this never returns None on a present id — but a *corrupt* projection row is
    exactly the drift this layer must survive: a non-UUID id is treated like
    any other uncitable payload (DROPPED, counted into the PARTIAL_RESULTS
    warning), never a ``ValueError`` that fails the whole query on one bad row
    (§22 degradation-not-failure). One parse point for every call site.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _payload_str(raw: object) -> str | None:
    """A payload DISPLAY field (text/title) as a str, or None if non-string.

    Same untrusted-payload discipline as :func:`_payload_uuid`, but for the
    OPTIONAL fields: a corrupt (non-string) ``text`` is coerced to None — the
    hit stays citable (its source_refs come from Postgres, unaffected) and only
    the display field is omitted — rather than emitted as a non-string that
    would make the whole §16 response schema-invalid (result ``text``/``title``
    are ``string|null``). The IDENTIFYING fields never come from an untrusted
    payload string: they are derived from the validated UUID, so a corrupt
    ``canonical_id`` can never reach ``RetrievalResult.id``."""
    return raw if isinstance(raw, str) else None


#: semantic_search's caller-selectable point types — the §4 index vocabulary.
_POINT_TYPES = ("chunk", "entity")


async def semantic_search(
    repo: BuildScopedRepo,
    vectors: BuildScopedVectorRepo,
    embedder: BaseEmbedding,
    query: str,
    top_k: int,
    point_type: str | None = None,
) -> McpResponse:
    """§8 semantic kNN over the active build, as a §16 response.

    ``repo`` and ``vectors`` must be bound to the same active
    ``(project, build_id)`` — the caller resolves the active build once and
    mints both (DR-001). ``embedder`` must be the model the index step used, so
    query and stored vectors share a space (§3); only
    :meth:`aget_text_embedding` is used. ``point_type`` narrows the search to
    one point type; when omitted BOTH types are searched with a per-type page
    floor (MCP6 — see :func:`_fair_page`).
    """
    if (repo.project, repo.build_id) != (vectors.project, vectors.build_id):
        raise ValueError(
            "repo and vector-repo scopes disagree "
            f"({repo.project}/{repo.build_id} vs {vectors.project}/{vectors.build_id}) — "
            "both must bind the same active build or enrichment would cross scopes"
        )
    if type(top_k) is not int or top_k < 1:
        # out-of-contract input degrades typed (§22), never a store error —
        # bool <: int is annotation-silent, and a non-positive limit would
        # reach Qdrant as an invalid search (the sibling-mode door guard,
        # C6d/C6e parity)
        return McpResponse(
            query=query,
            tool=_TOOL,
            project=repo.project,
            build_id=str(repo.build_id),
            results=(),
            warnings=(
                QueryWarning(
                    "GUARDRAIL_BLOCKED", f"top_k must be a positive integer, got {top_k!r}"
                ),
            ),
        )

    if point_type is not None and point_type not in _POINT_TYPES:
        return McpResponse(
            query=query,
            tool=_TOOL,
            project=repo.project,
            build_id=str(repo.build_id),
            results=(),
            warnings=(
                QueryWarning(
                    "GUARDRAIL_BLOCKED",
                    f"point_type must be one of {_POINT_TYPES} (or omitted for both), "
                    f"got {point_type!r}",
                ),
            ),
        )

    query_vector = await embedder.aget_text_embedding(query)
    if point_type is not None:
        hit_lists = [await vectors.search(query_vector, limit=top_k, point_type=point_type)]
    else:
        hit_lists = [
            await vectors.search(query_vector, limit=top_k, point_type="chunk"),
            await vectors.search(query_vector, limit=top_k, point_type="entity"),
        ]

    all_hits = [hit for hits in hit_lists for hit in hits]
    chunk_ids, entity_ids = _partition_hit_source_ids(all_hits)
    chunk_by_id, source_uri_by_chunk = await _load_chunk_provenance(repo, chunk_ids)
    mentions_by_entity = await repo.mentions_by_entity(list(entity_ids))
    types_by_entity = await _entity_types(repo, entity_ids)

    # EVERY fetched hit is validated before any page slot is allocated
    # (Codex #126): slicing the raw hits first let a drift-stale hit occupy a
    # floor slot and then drop at enrichment — evicting a fetched, perfectly
    # citable lower-ranked chunk and returning zero chunks despite one being
    # available. The drop count covers the whole fetched window: under the
    # quota any stale hit in it displaced the allocation, and the warning's
    # job is surfacing drift (§19/Health).
    validated_lists: list[list[RetrievalResult]] = []
    dropped = 0
    for hits in hit_lists:
        validated: list[RetrievalResult] = []
        for hit in hits:
            payload = hit.payload
            result = (
                _build_result(
                    payload,
                    hit.score,
                    chunk_by_id,
                    source_uri_by_chunk,
                    mentions_by_entity,
                    types_by_entity,
                )
                if payload is not None
                else None
            )
            if result is None:
                dropped += 1
            else:
                validated.append(result)
        validated_lists.append(validated)

    if point_type is not None:
        results = validated_lists[0][:top_k]
    else:
        results = _fair_page(validated_lists[0], validated_lists[1], top_k)

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
        point_type = payload.get("type")
        if point_type == "chunk" and (cid := _payload_uuid(payload.get("chunk_id"))) is not None:
            chunk_ids.add(cid)
        elif (
            point_type == "entity" and (eid := _payload_uuid(payload.get("entity_id"))) is not None
        ):
            entity_ids.add(eid)
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


def _fair_page(
    chunk_results: list[RetrievalResult],
    entity_results: list[RetrievalResult],
    top_k: int,
) -> list[RetrievalResult]:
    """MCP6 page fairness: chunk and entity points share ONE collection and
    one cosine, and the measured build skew (1405 entities vs 442 chunks —
    76% of the index) let bare name matches crowd every text passage off the
    page: 票價/海科館全票 returned 8 entities, 0 chunks. Each type gets a
    floor of ``top_k // 2`` slots (or all it has — a scarce type never
    blocks the other, the §22 over-block dual); the remaining slots go by
    score with the id tiebreak (#34: rerun-stable). Operates on CITABLE,
    SoR-validated results — never raw hits (Codex #126: a drift-stale hit
    in a floor slot would evict a fetched valid one). Final §16 ordering is
    re-imposed downstream by ``ordered_results``."""
    floor = top_k // 2
    take_chunks = min(floor, len(chunk_results))
    take_entities = min(floor, len(entity_results))
    page = chunk_results[:take_chunks] + entity_results[:take_entities]
    rest = sorted(
        chunk_results[take_chunks:] + entity_results[take_entities:],
        key=lambda result: (-result.score, result.id),
    )
    return page + rest[: top_k - len(page)]


async def _entity_types(repo: BuildScopedRepo, entity_ids: set[uuid.UUID]) -> dict[uuid.UUID, Any]:
    """Ontology type per entity hit, from the SoR (the payload carries only
    the point type). Measured (MCP6): 1405 active entities share 1285
    distinct names — the SAME name recurs across ontology types (主題館 =
    EVENT/EXHIBIT/FACILITY/LOCATION) with IDENTICAL scores, so without the
    type an agent can neither tell the copies apart nor dedup them."""
    if not entity_ids:
        return {}
    rows = await repo.fetch_all(tables.entities, tables.entities.c.id.in_(list(entity_ids)))
    return {row.id: row.type for row in rows}


def _build_result(
    payload: dict[str, Any],
    score: float,
    chunk_by_id: dict[uuid.UUID, Any],
    source_uri_by_chunk: dict[uuid.UUID, str | None],
    mentions_by_entity: dict[uuid.UUID, list[tuple[str, str]]],
    types_by_entity: dict[uuid.UUID, Any],
) -> RetrievalResult | None:
    """One hit → one §16 result, or None if it cannot be cited (drop it)."""
    point_type = payload.get("type")
    if point_type == "chunk":
        return _chunk_result(payload, score, chunk_by_id, source_uri_by_chunk)
    if point_type == "entity":
        return _entity_result(payload, score, mentions_by_entity, types_by_entity)
    return None  # an unknown point type cannot be mapped to a §16 result_type


def _chunk_result(
    payload: dict[str, Any],
    score: float,
    chunk_by_id: dict[uuid.UUID, Any],
    source_uri_by_chunk: dict[uuid.UUID, str | None],
) -> RetrievalResult | None:
    chunk_id = _payload_uuid(payload.get("chunk_id"))
    if chunk_id is None:
        return None
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
        text=_payload_str(payload.get("text")),
    )


def _entity_result(
    payload: dict[str, Any],
    score: float,
    mentions_by_entity: dict[uuid.UUID, list[tuple[str, str]]],
    types_by_entity: dict[uuid.UUID, Any],
) -> RetrievalResult | None:
    entity_id = _payload_uuid(payload.get("entity_id"))
    if entity_id is None:
        return None
    refs = tuple(
        SourceRef(source_type=source_type, id=source_ref)
        for kind, source_ref in mentions_by_entity.get(entity_id, [])
        if (source_type := _MENTION_SOURCE_TYPE.get(kind)) is not None
    )
    if not refs:
        return None  # §27.2 entity ref needs ≥1 chunk/row mention; none survived
    name = _payload_str(payload.get("text"))
    # the ontology type rides in the title (MCP6): the SAME name recurs
    # across types with identical scores, and §16's result shape has no type
    # field — the title is the one display slot that can tell them apart
    # (free string, no contract change). SoR type only; a missing row keeps
    # the bare name (never a coerced repr).
    etype = types_by_entity.get(entity_id)
    title = f"{name} ({etype})" if name is not None and isinstance(etype, str) and etype else name
    return RetrievalResult(
        result_type="entity",
        # id from the VALIDATED uuid, never the untrusted payload canonical_id:
        # index writes canonical_id == str(entity_id), so this is the same value
        # from a trusted source — a corrupt canonical_id cannot reach the id
        id=str(entity_id),
        score=score,
        source_refs=refs,
        title=title,
    )
