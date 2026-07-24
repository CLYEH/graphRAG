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
from collections.abc import Sequence
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
from core.stores import tables
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
    if not isinstance(params.entity, str) or not params.entity.strip():
        # the isinstance runs FIRST: a non-string (bad JSON tool input) would
        # AttributeError on .strip() — out-of-contract input degrades typed
        # (§22), it does not 500 (same rule as hops below)
        return "entity must be a non-blank canonical name string"
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
        if not isinstance(params.other_entity, str) or not params.other_entity.strip():
            # None and non-string both land here — isinstance first, like entity
            return "the path template needs other_entity (a non-blank name string)"
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
    deadline = time.monotonic() + policy.timeout_ms / 1000.0
    entities, dropped, truncated, timed_out, unresolved = await _neighbor_entities(
        graph, repo, policy, params, deadline
    )
    results = _score(entities)
    warnings = _standard_warnings(policy, truncated, dropped, timed_out)
    if unresolved:
        warnings = (*warnings, _unresolved_name_warning(params.entity))
    return _response(graph, query, results, warnings)


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
        # MCP2: name the side(s) that resolved to nothing — an empty envelope
        # here is otherwise read as "no path exists between two real entities"
        missing = [
            name
            for name, ids in ((params.entity, src_ids), (params.other_entity, dst_ids))
            if not ids
        ]
        return _response(
            graph, query, (), tuple(_unresolved_name_warning(name) for name in missing)
        )
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
        # a stale SHORTEST path must not mask a longer still-active path for
        # the SAME pair: after a candidate fails SoR verification, its stale
        # elements are EXCLUDED and the pair is retried — the exclusion
        # predicates are pushed into the shortestPath expansion (verified
        # live), so the next-shortest active path surfaces. Each retry grows
        # the exclusion set, so the inner loop terminates; the shared
        # deadline bounds it all the same.
        excluded_nodes: set[str] = set()
        excluded_edges: set[str] = set()
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms < 1:
                timed_out = True
                break
            found = await graph.shortest_path(
                str(src_id),
                str(dst_id),
                max_hops=params.hops,
                timeout_ms=remaining_ms,
                excluded_nodes=sorted(excluded_nodes),
                excluded_edges=sorted(excluded_edges),
            )
            if found is None:
                break  # no (further) path for this pair — next pair
            result, stale_nodes, stale_edges = await _verified_path_result(
                repo, found, src_id, dst_id
            )
            if result is not None:
                # a fully-verified path IS the complete answer — earlier stale
                # candidates were alternates, not omitted results, so no warning
                return _response(graph, query, ordered_results([result]), ())
            stale += 1  # this candidate failed SoR re-verification
            if not stale_nodes and not stale_edges:
                break  # unidentifiable (corrupt) elements — can't exclude, next pair
            excluded_nodes |= stale_nodes
            excluded_edges |= stale_edges
        if timed_out:
            break

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
) -> tuple[RetrievalResult | None, set[str], set[str]]:
    """One projection path → a fully SoR-verified §16 path result, or the
    STALE ELEMENTS that sank it (``(None, stale_node_ids, stale_edge_keys)``,
    exclusion-encoded) so the caller can retry the pair without them.

    A path is ONE claim: every projected value must parse, every node must
    still be active, and every edge must resolve to an active SoR relation —
    ANY stale hop rejects the whole candidate (§27.2/§19). Corrupt
    (unparseable) values return empty exclusion sets: they cannot be named, so
    the caller moves on rather than retrying the same path forever."""
    node_ids = [_projected_uuid(node.get("canonical_id")) for node in found["nodes"]]
    triples = [_edge_triple(rel) for rel in found["rels"]]
    if None in node_ids or None in triples:
        # corrupt projection values — the path can't be traced to the SoR
        return None, set(), set()
    ids = [node_id for node_id in node_ids if node_id is not None]
    clean = [triple for triple in triples if triple is not None]

    active = await repo.active_entity_ids(ids)
    resolved = await repo.relations_with_evidence(clean)
    stale_nodes = {str(node_id) for node_id in ids if node_id not in active}
    stale_edges = {
        f"{src}|{rel_type}|{dst}"  # the template's exclusion key, STORED direction
        for (src, dst, rel_type) in clean
        if (src, dst, rel_type) not in resolved
    }
    if stale_nodes or stale_edges:
        return None, stale_nodes, stale_edges  # stale in the SoR after projection

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
    result = RetrievalResult(
        result_type="path",
        id=f"path:{src_id}->{dst_id}",  # the pair that actually connected
        score=1.0,
        source_refs=refs,
        text="".join(parts),
    )
    return result, set(), set()


async def _subgraph(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    query: str,
) -> McpResponse:
    # the WHOLE subgraph phase — seed traversals AND the edge stage — shares
    # ONE policy deadline (per-stage timeouts would stack, the C6b trap)
    deadline = time.monotonic() + policy.timeout_ms / 1000.0
    entities, dropped, truncated, timed_out, unresolved = await _neighbor_entities(
        graph, repo, policy, params, deadline, include_seeds=True
    )
    # §21: max_rows is the ceiling on the WHOLE response, entities AND
    # relations combined — a dense neighborhood has O(n²) edges, so both the
    # edge FETCH (LIMIT in the store, +1 as the truncation probe) and the
    # emitted result list are capped; entities keep priority (nearest-first),
    # relations fill the remainder.
    node_ids = [entity_id for entity_id, _, _, _ in entities]
    edge_budget = policy.max_rows - len(entities)
    relations: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] = []
    edge_dropped = 0
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if timed_out or remaining_ms < 1:
        timed_out = True  # no budget left for the edge stage — surfaced below
    elif edge_budget > 0:
        edges = await graph.edges_among(
            [str(entity_id) for entity_id in node_ids],
            limit=edge_budget + 1,  # the truncation probe (policy cap only)
            timeout_ms=remaining_ms,
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
                [str(entity_id) for entity_id in node_ids], limit=1, timeout_ms=remaining_ms
            )
            truncated = truncated or bool(probe)
    results = _score(entities, relations)
    warnings = _standard_warnings(policy, truncated, dropped + edge_dropped, timed_out)
    if unresolved:
        warnings = (*warnings, _unresolved_name_warning(params.entity))
    return _response(graph, query, results, warnings)


@dataclass(frozen=True)
class SubgraphContext:
    """The frozen REST GraphContext payload (BA3c): nodes/edges as plain dicts
    in the GraphNode/GraphEdge shapes, every emitted value read from the SoR —
    the projection contributes topology only, exactly the graph_query
    discipline (#31/#33). No warnings channel exists on the REST response, so
    the §21/§22 caps still APPLY (via the policy) but truncation is not
    signalled — the client's own ``limit`` is the contract's cap channel."""

    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]


async def subgraph_context(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    seed: uuid.UUID,
    hops: int,
    *,
    max_graph_hops: int,
) -> SubgraphContext | None:
    """Id-seeded §21-governed subgraph for GET /graph/subgraph (BA3c).

    Same scope equality and hop-ceiling rules as :func:`graph_query` (both
    raise ``ValueError`` — the endpoint pre-validates for a clean 400; the
    raise here is the belt). Returns None when ``seed`` is not an ACTIVE
    entity of the bound build (the endpoint's 404). Traversal, SoR
    reachability recomputation, caps and the single deadline are all
    :func:`_neighbor_entities`'s — id-seeded via its ``seeds`` parameter — so
    the REST surface shows exactly the world the MCP graph tools show,
    including the §16 mention-backed rule (an active entity with no citable
    mention is dropped, even the seed). Edges are re-read from the SoR
    (id/src/dst/type/confidence from ``relations`` rows, active only, both
    endpoints inside the emitted node set) — never from projection values.
    """
    if graph.project != repo.project or graph.build_id != repo.build_id:
        raise ValueError(
            f"graph repo ({graph.project}, {graph.build_id}) and SoR repo "
            f"({repo.project}, {repo.build_id}) are bound to different scopes — "
            "emission would mix builds (DR-006)"
        )
    if hops < 1 or hops > max_graph_hops:
        raise ValueError(
            f"hops={hops} is outside the policy ceiling 1..{max_graph_hops} "
            "(§21 max_graph_hops) — rejected, not clamped"
        )
    if not await repo.active_entity_ids({seed}):
        return None

    deadline = time.monotonic() + policy.timeout_ms / 1000.0
    params = GraphQueryParams(template="subgraph", entity=str(seed), hops=hops)
    entities, _dropped, _truncated, timed_out, _unresolved = await _neighbor_entities(
        graph, repo, policy, params, deadline, include_seeds=True, seeds=[seed]
    )
    node_ids = [entity_id for entity_id, _, _, _ in entities]

    # node emission from the SoR rows (type + canonical_name as label);
    # nearest-first order preserved from the traversal
    node_rows = await repo.fetch_all(tables.entities, tables.entities.c.id.in_(node_ids))
    by_id = {row.id: row for row in node_rows}
    nodes = tuple(
        {
            "id": entity_id,
            "type": by_id[entity_id].type if entity_id in by_id else None,
            "label": by_id[entity_id].canonical_name if entity_id in by_id else None,
        }
        for entity_id in node_ids
    )

    # edge stage: same shared-deadline + row-budget discipline as _subgraph
    # (§21 max_rows ceils nodes AND edges combined; entities keep priority),
    # and the SAME §16 citability bar — a projected edge is emitted only when
    # its SoR relation is active AND carries ≥1 valid evidence (the
    # _relation_results doctrine; an evidence-less edge is invisible on every
    # surface, MCP and REST alike)
    edge_budget = policy.max_rows - len(nodes)
    edges: tuple[dict[str, Any], ...] = ()
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if not timed_out and remaining_ms >= 1 and edge_budget > 0 and len(node_ids) >= 2:
        projected = await graph.edges_among(
            [str(entity_id) for entity_id in node_ids],
            limit=edge_budget + 1,  # the truncation probe (policy cap only)
            timeout_ms=remaining_ms,
        )
        projected = projected[:edge_budget]
        triples = [t for edge in projected if (t := _edge_triple(edge)) is not None]
        resolved = await repo.relations_with_evidence(triples)
        citable = [
            relation_id
            for triple, (relation_id, evidence_rows) in resolved.items()
            if any(evidence_ref(row) is not None for row in evidence_rows)
        ]
        if citable:
            node_set = set(node_ids)
            edge_rows = await repo.fetch_all(tables.relations, tables.relations.c.id.in_(citable))
            edges = tuple(
                {
                    "id": row.id,
                    "src": row.src_entity_id,
                    "dst": row.dst_entity_id,
                    "type": row.type,
                    "confidence": row.confidence,
                }
                for row in sorted(edge_rows, key=lambda r: str(r.id))
                if row.src_entity_id in node_set and row.dst_entity_id in node_set
            )
    return SubgraphContext(nodes=nodes, edges=edges)


# -- shared emission helpers ---------------------------------------------------


async def _neighbor_entities(
    graph: BuildScopedGraphRepo,
    repo: BuildScopedRepo,
    policy: TextToCypher,
    params: GraphQueryParams,
    deadline: float,
    *,
    include_seeds: bool = False,
    seeds: Sequence[uuid.UUID] | None = None,
) -> tuple[list[tuple[uuid.UUID, int, tuple[SourceRef, ...], str | None]], int, bool, bool, bool]:
    """Traverse from every seed, merge, re-verify against the SoR.

    Returns ``(kept, dropped, truncated, timed_out, unresolved)`` where
    ``unresolved`` means the seed was NAME-seeded and resolved to nothing
    (MCP2: the caller must warn — zero rows from a name that matched no
    active entity is a different answer from an empty neighborhood, and only
    this flag tells the two apart); ``kept`` is
    ``[(entity_id, distance, mention_refs)]`` ordered nearest-first and capped
    at ``policy.max_rows`` — the probe row (one past the cap) exists only to
    detect the POLICY ceiling (TRUNCATED, §22); ``dropped`` counts hits the
    SoR re-verification rejected (§19 drift / corrupt projection values).

    Every seed traversal runs on the REMAINING budget of the caller's single
    ``deadline`` — per-seed timeouts would stack to seeds × timeout_ms (the
    C6b per-statement-vs-per-phase trap); running out mid-scan surfaces
    ``timed_out``, never a silently smaller neighborhood.

    The projection's TRAVERSAL is untrusted too, not just its values: an edge
    whose SoR relation moved off ``active`` after projection still exists in
    Neo4j and can reach an otherwise-active target. So reachability is
    RECOMPUTED here over the SoR's active nodes and relations (an undirected
    BFS bounded by ``hops``); candidates the active graph cannot reach are
    dropped as drift. (A candidate clipped from the node set by the fetch cap
    can make a genuinely-reachable node look unreachable — an over-drop, never
    an under-verify, and the clip itself is surfaced as TRUNCATED.)"""
    named = seeds is None
    if seeds is None:
        # name-seeded (the graph_query templates); BA3c passes SoR-verified ids
        seeds = await repo.entity_ids_by_name(params.entity)
    if not seeds:
        # MCP2: a name resolving to NOTHING is a different answer from a seed
        # with no neighbours, but both are zero rows — without this flag the
        # caller cannot tell them apart, and an agent asked "is A related to
        # B?" confidently answers "no" when it merely mistyped the name
        return [], 0, False, False, named

    candidates: set[uuid.UUID] = set()
    dropped = 0
    timed_out = False
    fetch_clipped = False
    for seed in seeds:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < 1:
            timed_out = True
            break
        rows = await graph.neighbors(
            str(seed),
            hops=params.hops,
            limit=policy.max_rows + 1,  # the truncation probe (policy cap only)
            timeout_ms=remaining_ms,
        )
        if len(rows) > policy.max_rows:
            # the store LIMIT was hit — later valid neighbors may exist beyond
            # it, so the clip is recorded at FETCH time: judging it after the
            # SoR drops would let drift shrink the count back under the cap
            # and silently suppress TRUNCATED
            fetch_clipped = True
        for row in rows:
            entity_id = _projected_uuid(row["entity"].get("canonical_id"))
            if entity_id is None:
                dropped += 1  # corrupt projection value — uncitable
            else:
                candidates.add(entity_id)

    # SoR reachability: BFS from the active seeds over ACTIVE relations only,
    # bounded by hops — the projected distances are recomputed, not trusted.
    node_set = set(seeds) | candidates
    active = await repo.active_entity_ids(node_set)
    pairs = await repo.active_relation_pairs_among(active)
    adjacency: dict[uuid.UUID, set[uuid.UUID]] = {}
    for src, dst in pairs:
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)  # traversal is undirected
    frontier = {seed for seed in seeds if seed in active}
    distances: dict[uuid.UUID, int] = dict.fromkeys(frontier, 0)
    for hop in range(1, params.hops + 1):
        frontier = {
            neighbor
            for node in frontier
            for neighbor in adjacency.get(node, ())
            if neighbor not in distances
        }
        for neighbor in frontier:
            distances[neighbor] = hop
        if not frontier:
            break

    best = {eid: dist for eid, dist in distances.items() if eid in candidates}
    if include_seeds:
        for seed in seeds:
            if seed in active:
                best[seed] = 0
    dropped += len(candidates - set(best))  # unreachable via the ACTIVE graph → drift

    ordered = sorted(best.items(), key=lambda item: (item[1], item[0]))
    truncated = fetch_clipped or len(ordered) > policy.max_rows
    ordered = ordered[: policy.max_rows]

    # SoR re-verification (§27.2): an entity result needs ≥1 mention of a
    # still-active entity; mentions_by_entity filters status='active', so a
    # drifted (non-active) node resolves to zero mentions and is dropped.
    mentions = await repo.mentions_by_entity([entity_id for entity_id, _ in ordered])
    names = await repo.active_entity_names([entity_id for entity_id, _ in ordered])
    kept: list[tuple[uuid.UUID, int, tuple[SourceRef, ...], str | None]] = []
    for entity_id, distance in ordered:
        refs = tuple(
            SourceRef(source_type=source_type, id=source_ref)
            for kind, source_ref in mentions.get(entity_id, [])
            if (source_type := _MENTION_SOURCE_TYPE.get(kind)) is not None
        )
        if refs:
            kept.append((entity_id, distance, refs, names.get(entity_id)))
        else:
            dropped += 1
    return kept, dropped, truncated, timed_out, False


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
    endpoint_ids = {t[0] for t in clean} | {t[1] for t in clean}
    names = await repo.active_entity_names(endpoint_ids)
    kept: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] = []
    for triple in clean:
        if triple not in resolved:
            dropped += 1  # stale projection edge — no active SoR relation
            continue
        relation_id, evidence_rows = resolved[triple]
        refs = tuple(ref for row in evidence_rows if (ref := evidence_ref(row)) is not None)
        if not refs:
            dropped += 1  # §27.2: a relation result cites ≥1 evidence; none survived
            continue
        # rendered like a one-hop path ("src -[type]-> dst") so consumers —
        # agents and §20 relation_hit_rate alike — read the edge from the
        # visible text; bare ids are unreadable to both
        src_name = names.get(triple[0], str(triple[0]))
        dst_name = names.get(triple[1], str(triple[1]))
        kept.append((relation_id, f"{src_name} -[{triple[2]}]-> {dst_name}", refs))
    return kept, dropped


def evidence_ref(row: dict[str, Any]) -> SourceRef | None:
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
    entities: list[tuple[uuid.UUID, int, tuple[SourceRef, ...], str | None]],
    relations: list[tuple[uuid.UUID, str, tuple[SourceRef, ...]]] | None = None,
) -> tuple[RetrievalResult, ...]:
    """Positional scores across [entities…, relations…] — graph hits carry no
    relevance ranking, but a strictly descending score keeps the traversal's
    own nearest-first order through ``ordered_results`` (score desc)."""
    total = len(entities) + len(relations or [])
    if total == 0:
        return ()
    results: list[RetrievalResult] = []
    for index, (entity_id, _, refs, name) in enumerate(entities):
        results.append(
            RetrievalResult(
                result_type="entity",
                id=str(entity_id),
                score=(total - index) / total,
                source_refs=refs,
                # the SoR's canonical name — visible text for consumers and
                # for §20 entity_recall (a bare uuid is unreadable to both)
                title=name,
            )
        )
    for offset, (relation_id, rendered, refs) in enumerate(relations or []):
        index = len(entities) + offset
        results.append(
            RetrievalResult(
                result_type="relation",
                id=str(relation_id),
                title=rendered,
                score=(total - index) / total,
                source_refs=refs,
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
    policy: TextToCypher, truncated: bool, dropped: int, timed_out: bool = False
) -> tuple[QueryWarning, ...]:
    warnings: list[QueryWarning] = []
    if truncated:
        warnings.append(
            _warn("TRUNCATED", f"result truncated to the {policy.max_rows}-row ceiling (§21)")
        )
    if dropped:
        warnings.append(_partial(dropped, "graph"))
    if timed_out:
        warnings.append(
            _warn(
                "PARTIAL_RESULTS",
                f"traversal exceeded the {policy.timeout_ms}ms deadline before every "
                "stage completed (§21)",
            )
        )
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


def _unresolved_name_warning(name: str) -> QueryWarning:
    """MCP2: the seed name matched no ACTIVE entity in this build.

    Zero rows is the same shape whether a name is unknown or its neighbourhood
    is genuinely empty, so the distinction has to be carried in a warning or
    the caller cannot recover: replaying the identical call is futile, while
    replaying a CORRECTED name succeeds. ``GUARDRAIL_BLOCKED`` is the frozen
    code for "this invocation produced nothing because the input was not
    usable" (the hops/template rejections' family) — no §27.2 bump needed.
    Name matching is exact (case-insensitive) equality, so the message points
    at ``get_entity``, which answers "does this name exist" unambiguously."""
    return QueryWarning(
        GUARDRAIL_WARNING_CODE,
        f"entity name {name!r} matched no active entity in this build — the "
        "traversal ran against no seed, so an empty result here does NOT mean "
        "'no relations exist'. Names match exactly (case-insensitive); confirm "
        "the canonical spelling with get_entity before retrying.",
    )


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
