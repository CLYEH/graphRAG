"""Graph retrieval: parameterized traversal templates → §16 response (§8/§21/§27.6, C6c).

The §8 ``graph`` modality, via §27.6's frozen default path: the three
parameterized templates (``GRAPH_QUERY_TEMPLATES`` — neighbors / path /
subgraph) executed by :class:`~core.stores.graph.BuildScopedGraphRepo`. There
is no free NL→Cypher here — no API in the chain accepts query text, so the
guardrail surface is parameter validation (template vocabulary, hop ceiling),
not a language grammar (the C6b lesson: a guarded language is the costliest
possible surface; a template vocabulary is the cheapest).

The traversal READS the Neo4j projection, which is UNTRUSTED (§19 forward-only
drift: nodes/edges can outlive their SoR exclusion) — so every value crossing
into the response is re-verified against Postgres or dropped (the C6a read/emit
rule): entity hits must resolve to ≥1 SoR mention of a still-active entity
(§27.2 entity minimum), every path/subgraph edge must resolve to a
still-``active`` SoR relation, and relation citations are built from the SoR's
own ``relation_evidence`` rows, each validated against the frozen §16 evidence
shapes before it is emitted. Drops are counted into ``PARTIAL_RESULTS`` (§22)
— degradation, never a silently complete-looking answer, never a 500.

Seeds are resolved in Postgres too (``entity_ids_by_name``): the projection
never decides what an entity IS, only how entities connect.
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass
from typing import Any

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from core.graph.structured import split_row_source_ref
from core.query.policy import (
    GRAPH_QUERY_TEMPLATES,
    GUARDRAIL_WARNING_CODE,
    TextToCypher,
)
from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)
from core.stores.graph import BuildScopedGraphRepo
from core.stores.repo import BuildScopedRepo

_TOOL = "graph_query"

#: entity_mentions.source_kind → §16 source_type (same mapping as C6a).
_MENTION_SOURCE_TYPE = {"text": "chunk", "structured": "row"}


@dataclass(frozen=True)
class GraphQueryParams:
    """One graph-tool invocation: which template, seeded where, how far.

    ``entity``/``other_entity`` are canonical names (resolved to ids in the
    SoR); ``other_entity`` is the path template's destination and meaningless
    elsewhere — a mismatch is rejected, not guessed at."""

    template: str
    entity: str
    other_entity: str | None = None
    hops: int = 1


async def graph_query(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    query: str,
    max_graph_hops: int,
) -> McpResponse:
    """§8 graph retrieval over the active build, as a §16 response.

    ``graph`` and ``repo`` are both bound to the active build (DR-001) —
    verified equal here, because emission mixes values from both and a split
    scope would cross builds (DR-006). ``policy`` carries the graph mode's row
    cap and deadline; ``max_graph_hops`` is the top-level hop ceiling (§21
    single source). ``query`` is the caller's original question, echoed into
    the response envelope.
    """
    if graph.project != repo.project or graph.build_id != repo.build_id:
        raise ValueError(
            f"graph repo ({graph.project}, {graph.build_id}) and SoR repo "
            f"({repo.project}, {repo.build_id}) are bound to different scopes — "
            "emission would mix builds (DR-006)"
        )

    blocked = _validate_params(params, max_graph_hops)
    if blocked is not None:
        return _response(graph, query, (), (_warn(GUARDRAIL_WARNING_CODE, blocked),))

    try:
        if params.template == "neighbors":
            return await _neighbors(graph, repo, policy, params, query)
        if params.template == "path":
            return await _path(graph, repo, policy, params, query)
        return await _subgraph(graph, repo, policy, params, query)
    except (Neo4jError, ServiceUnavailable) as exc:
        return _response(graph, query, (), (_degrade_warning(exc, policy.timeout_ms),))


def _validate_params(params: GraphQueryParams, max_graph_hops: int) -> str | None:
    """The template-path guardrail: vocabulary + parameter shape + hop ceiling.
    Returns the rejection reason, or None when the invocation is legal."""
    if params.template not in GRAPH_QUERY_TEMPLATES:
        return (
            f"unknown graph template {params.template!r} — the §27.6 vocabulary "
            f"is {list(GRAPH_QUERY_TEMPLATES)}"
        )
    if not params.entity.strip():
        return "entity must be a non-blank canonical name"
    if type(params.hops) is not int:
        # bool <: int is annotation-silent, and a str would TypeError below —
        # an out-of-contract hops degrades typed (§22), it does not 500
        return f"hops must be an integer, got {type(params.hops).__name__}"
    if params.hops < 1 or params.hops > max_graph_hops:
        return (
            f"hops={params.hops} is outside the policy ceiling 1..{max_graph_hops} "
            "(§21 max_graph_hops) — rejected, not clamped"
        )
    if params.template == "path":
        if params.other_entity is None or not params.other_entity.strip():
            return "the path template needs other_entity (the destination)"
    elif params.other_entity is not None:
        return f"other_entity is only meaningful for the path template, not {params.template!r}"
    return None


# -- the three templates -------------------------------------------------------


async def _neighbors(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    query: str,
) -> McpResponse:
    entities, dropped, truncated = await _neighbor_entities(graph, repo, policy, params)
    results = _score(entities)
    return _response(graph, query, results, _standard_warnings(policy, truncated, dropped))


async def _path(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    query: str,
) -> McpResponse:
    assert params.other_entity is not None  # _validate_params guarantees it
    src_ids = await repo.entity_ids_by_name(params.entity)
    dst_ids = await repo.entity_ids_by_name(params.other_entity)
    if not src_ids or not dst_ids:
        return _response(graph, query, (), ())
    # A name can resolve to SEVERAL active entities (distinct disambiguators,
    # §27.3) — try every (src, dst) pair in deterministic order until one
    # yields a path that SURVIVES SoR re-verification: a stale/corrupt
    # projection path rejects the CANDIDATE, not the search (a later pair may
    # hold a fully-active citable path). The WHOLE search shares ONE policy
    # deadline (each query gets only the remaining budget): per-pair timeouts
    # would stack to pairs × timeout_ms — the same per-statement-vs-per-phase
    # trap as C6b's schema discovery.
    deadline = time.monotonic() + policy.timeout_ms / 1000.0
    timed_out = False
    stale = 0
    for src_id, dst_id in itertools.product(src_ids, dst_ids):
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < 1:
            timed_out = True
            break
        found = await graph.shortest_path(
            str(src_id), str(dst_id), max_hops=params.hops, timeout_ms=remaining_ms
        )
        if found is None:
            continue
        result = await _verified_path_result(repo, found, src_id, dst_id)
        if result is not None:
            # a fully-verified path IS the complete answer — earlier stale
            # candidates were alternates, not omitted results, so no warning
            return _response(graph, query, ordered_results([result]), ())
        stale += 1  # this candidate failed SoR re-verification — try the next pair

    if timed_out:
        warning = _warn(
            "PARTIAL_RESULTS",
            f"path search exceeded the {policy.timeout_ms}ms deadline before "
            "every endpoint pair was tried (§21)",
        )
        return _response(graph, query, (), (warning,))
    if stale:
        return _response(graph, query, (), (_partial(stale, "path"),))
    return _response(graph, query, (), ())


async def _verified_path_result(
    repo: BuildScopedRepo, found: dict[str, Any], src_id: uuid.UUID, dst_id: uuid.UUID
) -> RetrievalResult | None:
    """One projection path → a fully SoR-verified §16 path result, or None.

    A path is ONE claim: every projected value must parse, every node must
    still be active, and every edge must resolve to an active SoR relation —
    ANY stale hop rejects the whole candidate (§27.2/§19)."""
    node_ids = [_projected_uuid(node.get("canonical_id")) for node in found["nodes"]]
    triples = [_edge_triple(rel) for rel in found["rels"]]
    if None in node_ids or None in triples:
        return None  # corrupt projection values — the path can't be traced to the SoR
    ids = [node_id for node_id in node_ids if node_id is not None]
    clean = [triple for triple in triples if triple is not None]

    active = await repo.active_entity_ids(ids)
    resolved = await repo.relations_with_evidence(clean)
    if len(active) != len(set(ids)) or any(triple not in resolved for triple in clean):
        return None  # a node or edge went stale in the SoR after projection

    refs = tuple(SourceRef(source_type="relation", id=str(resolved[triple][0])) for triple in clean)
    names = [_display(node) for node in found["nodes"]]
    # the pattern is UNDIRECTED, so a hop can traverse an edge against its
    # stored direction — each arrow is oriented by the SoR direction the rel
    # carries (src == the node we came from → forward), never by traversal
    # order alone, or the display would reverse the relation's meaning
    parts = [names[0]]
    for rel, prev, name in zip(found["rels"], found["nodes"], names[1:], strict=False):
        if rel.get("src") == prev.get("canonical_id"):
            parts.append(f" -[{rel['type']}]-> {name}")
        else:
            parts.append(f" <-[{rel['type']}]- {name}")
    return RetrievalResult(
        result_type="path",
        id=f"path:{src_id}->{dst_id}",  # the pair that actually connected
        score=1.0,
        source_refs=refs,
        text="".join(parts),
    )


async def _subgraph(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    query: str,
) -> McpResponse:
    entities, dropped, truncated = await _neighbor_entities(
        graph, repo, policy, params, include_seeds=True
    )
    # §21: max_rows is the ceiling on the WHOLE response, entities AND
    # relations combined — a dense neighborhood has O(n²) edges, so both the
    # edge FETCH (LIMIT in the store, +1 as the truncation probe) and the
    # emitted result list are capped; entities keep priority (nearest-first),
    # relations fill the remainder.
    node_ids = [entity_id for entity_id, _, _ in entities]
    edge_budget = policy.max_rows - len(entities)
    relations: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] = []
    edge_dropped = 0
    if edge_budget > 0:
        edges = await graph.edges_among(
            [str(entity_id) for entity_id in node_ids],
            limit=edge_budget + 1,  # the truncation probe (policy cap only)
            timeout_ms=policy.timeout_ms,
        )
        if len(edges) > edge_budget:
            # truncation is judged on the FETCHED count (like the neighbor
            # path), not post-drop — a stale edge dropping out must not hide
            # that the ceiling clipped the set
            truncated = True
            edges = edges[:edge_budget]
        relations, edge_dropped = await _relation_results(repo, edges)
    elif len(node_ids) >= 2:
        if params.hops == 1:
            # every distance-1 neighbor is directly adjacent to the seed, so
            # edges exist by construction and had no room — surfaced
            truncated = True
        else:
            # at hops ≥ 2 a neighbor can connect only through an excluded
            # intermediate, so direct edges may genuinely not exist — probe
            # (LIMIT 1) instead of asserting, keeping TRUNCATED exact
            probe = await graph.edges_among(
                [str(entity_id) for entity_id in node_ids], limit=1, timeout_ms=policy.timeout_ms
            )
            truncated = truncated or bool(probe)
    results = _score(entities, relations)
    warnings = _standard_warnings(policy, truncated, dropped + edge_dropped)
    return _response(graph, query, results, warnings)


# -- shared emission helpers ---------------------------------------------------


async def _neighbor_entities(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    *,
    include_seeds: bool = False,
) -> tuple[list[tuple[uuid.UUID, int, tuple[SourceRef, ...]]], int, bool]:
    """Traverse from every seed, merge, re-verify against the SoR.

    Returns ``(kept, dropped, truncated)`` where ``kept`` is
    ``[(entity_id, distance, mention_refs)]`` ordered nearest-first and capped
    at ``policy.max_rows`` — the probe row (one past the cap) exists only to
    detect the POLICY ceiling (TRUNCATED, §22); ``dropped`` counts hits the
    SoR re-verification rejected (§19 drift / corrupt projection values)."""
    seeds = await repo.entity_ids_by_name(params.entity)
    if not seeds:
        return [], 0, False

    best: dict[uuid.UUID, int] = {seed: 0 for seed in seeds} if include_seeds else {}
    dropped = 0
    for seed in seeds:
        rows = await graph.neighbors(
            str(seed),
            hops=params.hops,
            limit=policy.max_rows + 1,  # the truncation probe (policy cap only)
            timeout_ms=policy.timeout_ms,
        )
        for row in rows:
            entity_id = _projected_uuid(row["entity"].get("canonical_id"))
            if entity_id is None:
                dropped += 1  # corrupt projection value — uncitable
                continue
            distance = row["distance"]
            hop = distance if isinstance(distance, int) else params.hops
            best[entity_id] = min(best.get(entity_id, hop), hop)

    ordered = sorted(best.items(), key=lambda item: (item[1], item[0]))
    truncated = len(ordered) > policy.max_rows
    ordered = ordered[: policy.max_rows]

    # SoR re-verification (§27.2): an entity result needs ≥1 mention of a
    # still-active entity; mentions_by_entity filters status='active', so a
    # drifted (non-active) node resolves to zero mentions and is dropped.
    mentions = await repo.mentions_by_entity([entity_id for entity_id, _ in ordered])
    kept: list[tuple[uuid.UUID, int, tuple[SourceRef, ...]]] = []
    for entity_id, distance in ordered:
        refs = tuple(
            SourceRef(source_type=source_type, id=source_ref)
            for kind, source_ref in mentions.get(entity_id, [])
            if (source_type := _MENTION_SOURCE_TYPE.get(kind)) is not None
        )
        if refs:
            kept.append((entity_id, distance, refs))
        else:
            dropped += 1
    return kept, dropped, truncated


async def _relation_results(
    repo: BuildScopedRepo, edges: list[dict[str, Any]]
) -> tuple[list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]], int]:
    """Map projected edges to citable relation results via the SoR.

    Returns ``([(relation_id, label, evidence_refs)], dropped)`` — an edge
    whose SoR relation is gone/non-active (drift), or whose evidence rows all
    fail the frozen §16 shapes, is dropped and counted."""
    triples = [_edge_triple(edge) for edge in edges]
    clean = [triple for triple in triples if triple is not None]
    dropped = len(triples) - len(clean)
    resolved = await repo.relations_with_evidence(clean)
    kept: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] = []
    for triple in clean:
        if triple not in resolved:
            dropped += 1  # stale projection edge — no active SoR relation
            continue
        relation_id, evidence_rows = resolved[triple]
        refs = tuple(ref for row in evidence_rows if (ref := _evidence_ref(row)) is not None)
        if not refs:
            dropped += 1  # §27.2: a relation result cites ≥1 evidence; none survived
            continue
        kept.append((relation_id, triple[2], refs))
    return kept, dropped


def _evidence_ref(row: dict[str, Any]) -> SourceRef | None:
    """One relation_evidence row → a §16 evidence-backed ref, or None.

    The SoR columns are nullable where the frozen contract shapes are not
    (chunk needs uri+quote+offsets, manual/document needs uri+quote, row needs
    a splittable table+pk) — a row that cannot satisfy its shape is skipped
    rather than emitted invalid (§22: over-drop, never a schema-invalid
    response)."""
    kind = row.get("evidence_type")
    quote = row.get("quote")
    source_uri = row.get("source_uri")
    if kind == "chunk":
        start, end = row.get("start_offset"), row.get("end_offset")
        if (
            isinstance(quote, str)
            and 0 < len(quote) <= 512
            and isinstance(source_uri, str)
            and source_uri
            and isinstance(start, int)
            and start >= 0
            and isinstance(end, int)
            and end >= 0
        ):
            chunk_id = row.get("chunk_id")
            return SourceRef(
                source_type="chunk",
                id=str(chunk_id) if chunk_id is not None else str(row["evidence_ref"]),
                source_uri=source_uri,
                metadata={"quote": quote, "start_offset": start, "end_offset": end},
            )
        return None
    if kind == "manual":
        if (
            isinstance(quote, str)
            and 0 < len(quote) <= 512
            and isinstance(source_uri, str)
            and source_uri
        ):
            return SourceRef(
                source_type="document",
                id=str(row["evidence_ref"]),
                source_uri=source_uri,
                metadata={"quote": quote},
            )
        return None
    if kind == "row":
        ref = row.get("evidence_ref")
        if not isinstance(ref, str):
            return None
        parts = split_row_source_ref(ref)
        if parts is not None and parts[0] and parts[1]:
            return SourceRef(
                source_type="row",
                id=ref,
                metadata={"table": parts[0], "pk": parts[1]},
            )
        return None
    return None  # out-of-vocabulary evidence_type — uncitable


def _score(
    entities: list[tuple[uuid.UUID, int, tuple[SourceRef, ...]]],
    relations: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] | None = None,
) -> tuple[RetrievalResult, ...]:
    """Positional scores across [entities…, relations…] — graph hits carry no
    relevance ranking, but a strictly descending score keeps the traversal's
    own nearest-first order through ``ordered_results`` (score desc)."""
    total = len(entities) + len(relations or [])
    if total == 0:
        return ()
    results: list[RetrievalResult] = []
    for index, (entity_id, _, refs) in enumerate(entities):
        results.append(
            RetrievalResult(
                result_type="entity",
                id=str(entity_id),
                score=(total - index) / total,
                source_refs=refs,
            )
        )
    for offset, (relation_id, rel_type, refs) in enumerate(relations or []):
        index = len(entities) + offset
        results.append(
            RetrievalResult(
                result_type="relation",
                id=str(relation_id),
                score=(total - index) / total,
                source_refs=refs,
                title=rel_type,
            )
        )
    return ordered_results(results)


def _projected_uuid(value: Any) -> uuid.UUID | None:
    """Parse an untrusted projected canonical_id — corrupt → None (drop)."""
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _edge_triple(rel: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """A projected edge's SoR identity — (src, dst, type), or None if corrupt."""
    src = _projected_uuid(rel.get("src"))
    dst = _projected_uuid(rel.get("dst"))
    rel_type = rel.get("type")
    if src is None or dst is None or not isinstance(rel_type, str) or not rel_type:
        return None
    return (src, dst, rel_type)


def _display(node: dict[str, Any]) -> str:
    """A node's display label: its name when it is a usable string, else its
    canonical_id — never a coerced repr (§16 values must BE strings)."""
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return name
    return str(node.get("canonical_id", "?"))


def _standard_warnings(
    policy: TextToCypher, truncated: bool, dropped: int
) -> tuple[QueryWarning, ...]:
    warnings: list[QueryWarning] = []
    if truncated:
        warnings.append(
            _warn("TRUNCATED", f"result truncated to the {policy.max_rows}-row ceiling (§21)")
        )
    if dropped:
        warnings.append(_partial(dropped, "graph"))
    return tuple(warnings)


def _partial(count: int, what: str) -> QueryWarning:
    return _warn(
        "PARTIAL_RESULTS",
        f"{count} {what} hit(s) omitted — stale or corrupt projection against the SoR (§19/§22)",
    )


def _degrade_warning(exc: Neo4jError | ServiceUnavailable, timeout_ms: int) -> QueryWarning:
    """Map a store failure to a typed degradation (§22), never a 500."""
    code = getattr(exc, "code", None) or ""
    if "TimedOut" in code or "Timeout" in code:
        return _warn("PARTIAL_RESULTS", f"traversal exceeded the {timeout_ms}ms deadline (§21)")
    return _warn("STORE_UNAVAILABLE", f"graph store unavailable ({type(exc).__name__})")


def _warn(code: str, message: str) -> QueryWarning:
    return QueryWarning(code, message)


def _response(
    graph: BuildScopedGraphRepo,
    query: str,
    results: tuple[RetrievalResult, ...],
    warnings: tuple[QueryWarning, ...],
) -> McpResponse:
    return McpResponse(
        query=query,
        tool=_TOOL,
        project=graph.project,
        build_id=str(graph.build_id),
        results=results,
        warnings=warnings,
    )
