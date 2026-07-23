"""Why: graph_query is the §27.6 default graph path — parameterized templates
over an UNTRUSTED forward-only projection, emitting the frozen §16 contract.
What must hold is not "the traversal works" (integration covers that) but the
emission discipline: the parameter guardrail rejects loud (typed, §22), every
projected value is re-verified against the SoR or DROPPED and counted, every
citation satisfies its frozen per-result_type minimum (§27.2), and a store
failure degrades to a typed warning — never a 500, never a schema-invalid
response. Each response here is validated against the frozen schema itself.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable

from core.query.graph import GraphQueryParams, graph_query, subgraph_context
from core.query.policy import CYPHER_ALLOWED_CLAUSES, CYPHER_BLOCKED_MIN, TextToCypher
from core.query.results import McpResponse
from core.stores.graph import BuildScopedGraphRepo
from core.stores.repo import BuildScopedRepo

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_PROJECT = "acme"
_BUILD = uuid.UUID("7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")

_POLICY = TextToCypher(
    enabled=False,  # templates run regardless — enabled gates only free NL→Cypher
    allowed_clauses=CYPHER_ALLOWED_CLAUSES,
    blocked=CYPHER_BLOCKED_MIN,
    max_rows=10,
    timeout_ms=2000,
)


class _FakeGraph:
    """Canned traversal results; records calls so caps/deadlines can be pinned."""

    def __init__(
        self,
        neighbor_rows: list[dict[str, Any]] | None = None,
        path: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
        paths_by_pair: dict[tuple[str, str], Any] | None = None,  # dict or list-of-dicts
        slow: bool = False,
    ) -> None:
        self.project = _PROJECT
        self.build_id = _BUILD
        self._neighbors = neighbor_rows or []
        self._path = path
        self._paths_by_pair = paths_by_pair
        self._edges = edges or []
        self._raise = raise_exc
        self._slow = slow
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def neighbors(
        self, seed: str, *, hops: int, limit: int, timeout_ms: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("neighbors", {"seed": seed, "hops": hops, "limit": limit}))
        if self._raise is not None:
            raise self._raise
        if self._slow:
            await asyncio.sleep(0.002)  # a real cost per scan, so the deadline test can bite
        return self._neighbors

    async def shortest_path(
        self,
        src: str,
        dst: str,
        *,
        max_hops: int,
        timeout_ms: int,
        excluded_nodes: tuple[str, ...] = (),
        excluded_edges: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        self.calls.append(
            (
                "shortest_path",
                {
                    "src": src,
                    "dst": dst,
                    "max_hops": max_hops,
                    "timeout_ms": timeout_ms,
                    "excluded_nodes": tuple(excluded_nodes),
                    "excluded_edges": tuple(excluded_edges),
                },
            )
        )
        if self._raise is not None:
            raise self._raise
        if self._paths_by_pair is not None:
            # a real (tiny) cost per attempt, so the deadline test can bite
            await asyncio.sleep(0.002)
            canned = self._paths_by_pair.get((src, dst))
            options: list[dict[str, Any]] = (
                canned if isinstance(canned, list) else [canned] if canned else []
            )
            for path in options:
                # mimic the template's exclusion pushdown: shortest first,
                # skipping any path touching an excluded node/edge
                nodes_ok = all(n.get("canonical_id") not in excluded_nodes for n in path["nodes"])
                edges_ok = all(
                    f"{r['src']}|{r['type']}|{r['dst']}" not in excluded_edges for r in path["rels"]
                )
                if nodes_ok and edges_ok:
                    return path
            return None
        return self._path

    async def edges_among(
        self, canonical_ids: list[str], *, limit: int, timeout_ms: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("edges_among", {"ids": list(canonical_ids), "limit": limit}))
        return self._edges[:limit]  # the store caps the fetch, like the template's LIMIT


class _FakeSoR:
    """The Postgres side: seeds, mentions, active-status, relations+evidence."""

    def __init__(
        self,
        seeds: dict[str, list[uuid.UUID]] | None = None,
        mentions: dict[uuid.UUID, list[tuple[str, str]]] | None = None,
        active: set[uuid.UUID] | None = None,
        relations: dict[tuple[uuid.UUID, uuid.UUID, str], tuple[uuid.UUID, list[dict[str, Any]]]]
        | None = None,
        pairs: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
    ) -> None:
        self.project = _PROJECT
        self.build_id = _BUILD
        self._seeds = seeds or {}
        self._mentions = mentions or {}
        self._active = active
        self._relations = relations or {}
        # the SoR edge set for reachability; defaults to the relations' keys so
        # relation-bearing fixtures stay consistent without repeating themselves
        self._pairs = (
            pairs if pairs is not None else {(src, dst) for (src, dst, _rtype) in self._relations}
        )

    async def entity_ids_by_name(self, name: str) -> list[uuid.UUID]:
        return self._seeds.get(name.lower(), [])

    async def mentions_by_entity(
        self, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, str]]]:
        return {eid: refs for eid, refs in self._mentions.items() if eid in entity_ids}

    async def active_entity_names(self, entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        # names mirror ids in the fake — presence is what the tests pin
        return {eid: f"name-{eid}" for eid in entity_ids}

    async def active_entity_ids(self, entity_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if self._active is not None:
            return {eid for eid in entity_ids if eid in self._active}
        return set(entity_ids)  # default: everything still active

    async def relations_with_evidence(
        self, triples: list[tuple[uuid.UUID, uuid.UUID, str]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID, str], tuple[uuid.UUID, list[dict[str, Any]]]]:
        return {t: self._relations[t] for t in triples if t in self._relations}

    async def active_relation_pairs_among(
        self, entity_ids: set[uuid.UUID]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return {(s, d) for (s, d) in self._pairs if s in entity_ids and d in entity_ids}


def _node(entity_id: uuid.UUID, name: str = "N") -> dict[str, Any]:
    return {
        "canonical_id": str(entity_id),
        "name": name,
        "type": "t",
        "status": "active",
        "build_id": str(_BUILD),
        "project": _PROJECT,
    }


def _chunk_evidence(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "evidence_type": "chunk",
        "evidence_ref": "chunk-ref-1",
        "chunk_id": uuid.uuid4(),
        "start_offset": 0,
        "end_offset": 12,
        "quote": "acme hired bob",
        "source_uri": "file:///a.txt",
    }
    row.update(overrides)
    return row


async def _run(
    graph: _FakeGraph, sor: _FakeSoR, params: GraphQueryParams, max_hops: int = 3
) -> McpResponse:
    response = await graph_query(
        cast(BuildScopedGraphRepo, graph),
        cast(BuildScopedRepo, sor),
        _POLICY,
        params,
        "the question",
        max_hops,
    )
    _VALIDATOR.validate(response.to_dict())  # every response is contract-valid
    return response


def _codes(response: McpResponse) -> list[str]:
    return [w.code for w in response.warnings]


# -- the parameter guardrail (§21: typed rejection, never execution) -----------


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        (GraphQueryParams(template="drop_all", entity="acme"), "unknown graph template"),
        (GraphQueryParams(template="neighbors", entity="   "), "non-blank canonical name"),
        (
            GraphQueryParams(template="neighbors", entity=123),  # type: ignore[arg-type]
            "non-blank canonical name string",
        ),
        (
            GraphQueryParams(template="path", entity="acme", other_entity=123),  # type: ignore[arg-type]
            "non-blank name string",
        ),
        (GraphQueryParams(template="neighbors", entity="acme", hops=0), "outside the policy"),
        (GraphQueryParams(template="neighbors", entity="acme", hops=4), "outside the policy"),
        (GraphQueryParams(template="path", entity="acme"), "needs other_entity"),
        (GraphQueryParams(template="neighbors", entity="acme", hops=True), "must be an integer"),
        (
            GraphQueryParams(template="neighbors", entity="acme", hops="2"),  # type: ignore[arg-type]
            "must be an integer",
        ),
        (
            GraphQueryParams(template="neighbors", entity="acme", other_entity="bob"),
            "only meaningful for the path template",
        ),
    ],
)
async def test_the_parameter_guardrail_rejects_loud_and_typed(
    params: GraphQueryParams, reason: str
) -> None:
    """The template path's whole guardrail surface is parameters (no query
    text exists to guard) — each illegal shape is rejected with the typed
    GUARDRAIL_BLOCKED and a checkable reason; nothing reaches the store."""
    graph = _FakeGraph()
    response = await _run(graph, _FakeSoR(), params)
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]
    assert reason in response.warnings[0].message
    assert graph.calls == []  # rejected before any traversal


async def test_mismatched_scopes_fail_loud() -> None:
    """Emission mixes graph and SoR values — bound to different builds they
    would cross versions (DR-006), so the mismatch is a bug, not a warning."""
    graph = _FakeGraph()
    graph.build_id = uuid.uuid4()  # not the SoR's build
    with pytest.raises(ValueError, match="different scopes"):
        await graph_query(
            cast(BuildScopedGraphRepo, graph),
            cast(BuildScopedRepo, _FakeSoR()),
            _POLICY,
            GraphQueryParams(template="neighbors", entity="acme"),
            "q",
            3,
        )


# -- neighbors ------------------------------------------------------------------


async def test_neighbors_returns_cited_entities_nearest_first() -> None:
    seed, near, far = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[
            {"entity": _node(far), "distance": 2},
            {"entity": _node(near), "distance": 1},
        ]
    )
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={
            near: [("text", str(uuid.uuid4()))],
            far: [("structured", "6:orders:17")],
        },
        pairs={(seed, near), (near, far)},  # the SoR agrees the traversal exists
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme", hops=2))
    assert response.warnings == ()
    assert [r.id for r in response.results] == [str(near), str(far)]  # nearest first
    assert response.results[0].source_refs[0].source_type == "chunk"
    assert response.results[1].source_refs[0].source_type == "row"
    assert response.results[0].result_type == "entity"
    # the SoR canonical name rides as title (C10: §20 entity_recall reads
    # visible text; agents read names, not bare uuids) — the fake returns
    # name-{id}, so a reverted title emission fails here
    assert response.results[0].title == f"name-{near}"


async def test_a_hit_without_sor_mentions_is_dropped_as_drift() -> None:
    """The projection is forward-only: a node whose entity moved off active
    resolves to zero mentions (mentions_by_entity is status-gated) — dropped
    AND surfaced as PARTIAL_RESULTS, never emitted uncited (§27.2/§19)."""
    seed, ghost, ok = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[
            {"entity": _node(ghost), "distance": 1},
            {"entity": _node(ok), "distance": 1},
        ]
    )
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={ok: [("text", "c-1")]},
        pairs={(seed, ghost), (seed, ok)},
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert [r.id for r in response.results] == [str(ok)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_a_corrupt_projected_canonical_id_is_dropped_not_crashed() -> None:
    seed, ok = uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[
            {"entity": {"canonical_id": "not-a-uuid", "name": "?"}, "distance": 1},
            {"entity": _node(ok), "distance": 1},
        ]
    )
    sor = _FakeSoR(seeds={"acme": [seed]}, mentions={ok: [("text", "c-1")]}, pairs={(seed, ok)})
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert [r.id for r in response.results] == [str(ok)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_the_policy_row_cap_truncates_and_flags() -> None:
    """max_rows+1 rows come back (the probe) → clipped to max_rows and flagged
    TRUNCATED (§22) — the §21 policy ceiling, not a caller choice."""
    seed = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(_POLICY.max_rows + 1)]
    graph = _FakeGraph(neighbor_rows=[{"entity": _node(eid), "distance": 1} for eid in ids])
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={eid: [("text", f"c-{eid}")] for eid in ids},
        pairs={(seed, eid) for eid in ids},
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert len(response.results) == _POLICY.max_rows
    assert _codes(response) == ["TRUNCATED"]


async def test_a_neighbor_reached_only_through_a_stale_edge_is_dropped() -> None:
    """The projection's TRAVERSAL is untrusted, not just its values: an edge
    whose SoR relation was rejected after projection still exists in Neo4j and
    can reach an otherwise-active target. Reachability is recomputed over the
    SoR's active relations — a target with no active path is drift, dropped and
    surfaced, never emitted as a valid graph neighbor."""
    seed, via_stale, via_live = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[  # the projection still reaches BOTH
            {"entity": _node(via_stale), "distance": 1},
            {"entity": _node(via_live), "distance": 1},
        ]
    )
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={
            via_stale: [("text", "c-stale")],  # active entity, still mentioned…
            via_live: [("text", "c-live")],
        },
        pairs={(seed, via_live)},  # …but the SoR edge to it is GONE
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert [r.id for r in response.results] == [str(via_live)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_the_seed_scan_shares_one_policy_deadline() -> None:
    """A name resolving to many entities must not multiply the latency cap:
    each seed traversal gets only the REMAINING budget of one deadline (the
    C6b per-statement-vs-per-phase lesson), and running out surfaces
    PARTIAL_RESULTS rather than a silently smaller neighborhood."""
    seeds = [uuid.uuid4() for _ in range(5)]
    tight = TextToCypher(
        enabled=False,
        allowed_clauses=CYPHER_ALLOWED_CLAUSES,
        blocked=CYPHER_BLOCKED_MIN,
        max_rows=10,
        timeout_ms=1,  # the whole scan's budget — attempts cost ~2ms each
    )
    graph = _FakeGraph(neighbor_rows=[], slow=True)
    sor = _FakeSoR(seeds={"s": sorted(seeds, key=str)})
    response = await graph_query(
        cast(BuildScopedGraphRepo, graph),
        cast(BuildScopedRepo, sor),
        tight,
        GraphQueryParams(template="neighbors", entity="s"),
        "q",
        3,
    )
    _VALIDATOR.validate(response.to_dict())
    assert "PARTIAL_RESULTS" in _codes(response)
    assert "deadline" in response.warnings[-1].message
    scans = [c for c in graph.calls if c[0] == "neighbors"]
    assert len(scans) < 5  # the deadline stopped the seed scan early


async def test_an_unknown_seed_yields_an_empty_result_not_an_error() -> None:
    """Still a typed §16 response, never an exception — but MCP2 adds the
    warning that says WHY it is empty (see the dedicated test below); the
    silent-empty this used to assert is the confidently-wrong-answer trap."""
    graph = _FakeGraph()
    response = await _run(graph, _FakeSoR(), GraphQueryParams(template="neighbors", entity="acme"))
    assert response.results == ()
    assert _codes(response) == ["GUARDRAIL_BLOCKED"]
    assert graph.calls == []  # no seed → nothing to traverse


# -- path -------------------------------------------------------------------------


def _path_fixture() -> tuple[_FakeGraph, _FakeSoR, uuid.UUID, uuid.UUID, uuid.UUID]:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rel_ab, rel_bc = uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        path={
            "nodes": [_node(a, "A"), _node(b, "B"), _node(c, "C")],
            "rels": [
                {"type": "works_at", "src": str(a), "dst": str(b)},
                {"type": "owns", "src": str(b), "dst": str(c)},
            ],
        }
    )
    sor = _FakeSoR(
        seeds={"a": [a], "c": [c]},
        relations={
            (a, b, "works_at"): (rel_ab, [_chunk_evidence()]),
            (b, c, "owns"): (rel_bc, [_chunk_evidence()]),
        },
    )
    return graph, sor, a, b, c


async def test_path_cites_every_edge_with_its_sor_relation() -> None:
    graph, sor, a, b, c = _path_fixture()
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert len(response.results) == 1
    result = response.results[0]
    assert result.result_type == "path"
    assert len(result.source_refs) == 2  # one ref PER EDGE (§27.2)
    assert all(ref.source_type == "relation" for ref in result.source_refs)
    assert result.text == "A -[works_at]-> B -[owns]-> C"
    assert response.warnings == ()


async def test_a_backward_traversed_edge_renders_its_stored_direction() -> None:
    """The path pattern is undirected, so a hop can walk an edge AGAINST its
    stored direction — the display arrow must follow the SoR direction the rel
    carries, not the traversal order, or 'B supplies C' would print as C
    supplying B (a reversed claim over correctly-cited evidence)."""
    b, a, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rel_ab, rel_ac = uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        path={
            # traversal B → A → C, but the FIRST edge is stored A→B (walked backward)
            "nodes": [_node(b, "B"), _node(a, "A"), _node(c, "C")],
            "rels": [
                {"type": "owns", "src": str(a), "dst": str(b)},  # stored A→B
                {"type": "funds", "src": str(a), "dst": str(c)},  # stored A→C
            ],
        }
    )
    sor = _FakeSoR(
        seeds={"b": [b], "c": [c]},
        relations={
            (a, b, "owns"): (rel_ab, [_chunk_evidence()]),
            (a, c, "funds"): (rel_ac, [_chunk_evidence()]),
        },
    )
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="b", other_entity="c", hops=3)
    )
    assert len(response.results) == 1
    assert response.results[0].text == "B <-[owns]- A -[funds]-> C"


async def test_a_path_with_a_stale_edge_is_dropped_whole() -> None:
    """A path is ONE claim: any hop whose SoR relation is gone/non-active makes
    the whole claim unciteable — dropped + PARTIAL_RESULTS, not emitted with a
    hole in its citations."""
    graph, sor, a, b, c = _path_fixture()
    sor._relations.pop((b, c, "owns"))  # the second hop went stale in the SoR
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert response.results == ()
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_a_path_through_a_non_active_node_is_dropped_whole() -> None:
    graph, sor, a, b, c = _path_fixture()
    sor._active = {a, c}  # b moved off active in the SoR after projection
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert response.results == ()
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_path_tries_every_endpoint_pair_until_one_connects() -> None:
    """A name resolves to several active entities (distinct disambiguators,
    §27.3) — a path query must search the resolved pairs, not silently give up
    because the FIRST pair happens to be unconnected."""
    miss, hit = sorted([uuid.uuid4(), uuid.uuid4()], key=str)  # tried in THIS order
    d1 = uuid.uuid4()
    rel = uuid.uuid4()
    connected = {
        "nodes": [_node(hit, "S2"), _node(d1, "D1")],
        "rels": [{"type": "works_at", "src": str(hit), "dst": str(d1)}],
    }
    graph = _FakeGraph(paths_by_pair={(str(hit), str(d1)): connected})
    sor = _FakeSoR(
        seeds={"s": [miss, hit], "d": [d1]},  # the connected pair is SECOND
        relations={(hit, d1, "works_at"): (rel, [_chunk_evidence()])},
    )
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="s", other_entity="d", hops=3)
    )
    assert len(response.results) == 1
    assert response.results[0].id == f"path:{hit}->{d1}"  # the pair that connected
    pair_calls = [c for c in graph.calls if c[0] == "shortest_path"]
    assert len(pair_calls) == 2  # first pair tried and missed, second connected


async def test_a_stale_candidate_path_does_not_end_the_search() -> None:
    """SoR re-verification rejects the CANDIDATE, not the search: if the first
    pair's projection path is stale (edge gone in the SoR) but a later pair
    holds a fully-active citable path, the valid path must be returned — not
    PARTIAL_RESULTS for the stale one."""
    miss, hit = sorted([uuid.uuid4(), uuid.uuid4()], key=str)  # tried in THIS order
    d1 = uuid.uuid4()
    rel = uuid.uuid4()
    stale_path = {
        "nodes": [_node(miss, "S1"), _node(d1, "D1")],
        "rels": [{"type": "gone", "src": str(miss), "dst": str(d1)}],  # no SoR relation
    }
    valid_path = {
        "nodes": [_node(hit, "S2"), _node(d1, "D1")],
        "rels": [{"type": "works_at", "src": str(hit), "dst": str(d1)}],
    }
    graph = _FakeGraph(
        paths_by_pair={(str(miss), str(d1)): stale_path, (str(hit), str(d1)): valid_path}
    )
    sor = _FakeSoR(
        seeds={"s": [miss, hit], "d": [d1]},
        relations={(hit, d1, "works_at"): (rel, [_chunk_evidence()])},  # only the SECOND resolves
    )
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="s", other_entity="d", hops=3)
    )
    assert len(response.results) == 1
    assert response.results[0].id == f"path:{hit}->{d1}"  # the verified pair, not the stale one
    assert response.warnings == ()  # a verified answer is complete; the stale one was an alternate


async def test_a_stale_shortest_path_does_not_mask_a_longer_active_path() -> None:
    """shortestPath returns ONE path — the projection's shortest. When that
    path is stale (its relation was rejected post-projection) but a LONGER
    fully-active path exists for the SAME pair, the stale elements are
    excluded and the pair retried, so the active path surfaces — not
    PARTIAL_RESULTS for a connection the active graph genuinely has."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rel_ab, rel_bc = uuid.uuid4(), uuid.uuid4()
    stale_short = {
        "nodes": [_node(a, "A"), _node(c, "C")],
        "rels": [{"type": "short", "src": str(a), "dst": str(c)}],  # no SoR relation
    }
    long_valid = {
        "nodes": [_node(a, "A"), _node(b, "B"), _node(c, "C")],
        "rels": [
            {"type": "r1", "src": str(a), "dst": str(b)},
            {"type": "r2", "src": str(b), "dst": str(c)},
        ],
    }
    graph = _FakeGraph(paths_by_pair={(str(a), str(c)): [stale_short, long_valid]})
    sor = _FakeSoR(
        seeds={"a": [a], "c": [c]},
        relations={
            (a, b, "r1"): (rel_ab, [_chunk_evidence()]),
            (b, c, "r2"): (rel_bc, [_chunk_evidence()]),
        },
    )
    response = await _run(
        graph, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert len(response.results) == 1
    assert response.results[0].text == "A -[r1]-> B -[r2]-> C"  # the longer ACTIVE path
    assert response.warnings == ()  # a verified answer is complete
    retry = [call for call in graph.calls if call[0] == "shortest_path"][1]
    assert f"{a}|short|{c}" in retry[1]["excluded_edges"]  # the stale edge was excluded


async def test_truncation_survives_sor_drops() -> None:
    """The store LIMIT clip is recorded at FETCH time: when the probe row came
    back AND drift drops shrink the survivors under the cap, TRUNCATED must
    still fire — otherwise an incomplete traversal (later valid neighbors
    hidden by the LIMIT) reads as merely drifted."""
    seed = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(_POLICY.max_rows + 1)]  # the probe row came back
    graph = _FakeGraph(neighbor_rows=[{"entity": _node(eid), "distance": 1} for eid in ids])
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={eid: [("text", f"c-{eid}")] for eid in ids},
        pairs={(seed, eid) for eid in ids[1:]},  # the first candidate's edge went stale
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert len(response.results) == _POLICY.max_rows  # survivors exactly fill the cap
    codes = _codes(response)
    assert "TRUNCATED" in codes and "PARTIAL_RESULTS" in codes  # both facts surfaced


async def test_the_pair_search_shares_one_policy_deadline() -> None:
    """Per-pair timeouts would stack to pairs × timeout_ms (the C6b
    per-statement-vs-per-phase lesson) — the search gets ONE deadline: each
    attempt runs on the REMAINING budget, and running out surfaces
    PARTIAL_RESULTS rather than silently reporting 'no path'."""
    srcs = [uuid.uuid4() for _ in range(5)]
    dst = uuid.uuid4()
    tight = TextToCypher(
        enabled=False,
        allowed_clauses=CYPHER_ALLOWED_CLAUSES,
        blocked=CYPHER_BLOCKED_MIN,
        max_rows=10,
        timeout_ms=1,  # the whole search budget — attempts cost ~2ms each
    )
    graph = _FakeGraph(paths_by_pair={})  # nothing connects; every attempt burns time
    sor = _FakeSoR(seeds={"s": sorted(srcs, key=str), "d": [dst]})
    response = await graph_query(
        cast(BuildScopedGraphRepo, graph),
        cast(BuildScopedRepo, sor),
        tight,
        GraphQueryParams(template="path", entity="s", other_entity="d", hops=3),
        "q",
        3,
    )
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["PARTIAL_RESULTS"]
    assert "deadline" in response.warnings[0].message
    pair_calls = [c for c in graph.calls if c[0] == "shortest_path"]
    assert len(pair_calls) < 5  # the deadline stopped the scan early
    assert all(c[1]["timeout_ms"] <= tight.timeout_ms for c in pair_calls)  # remaining budget only


async def test_an_unresolved_name_is_distinguishable_from_a_genuinely_empty_result() -> None:
    """MCP2 — the two zero-row answers must NOT look alike.

    A name that matches no active entity and a real entity with no path are
    both `results == ()`. Left identical (the behaviour this test previously
    pinned), an agent asked "is A related to B?" answers "no, they are not
    related" when it merely mistyped a name — a confidently wrong answer, and
    the one failure the caller could actually have recovered from by retrying
    with a corrected name. So the unresolved case now carries a typed warning
    naming the offending string; the genuinely-empty case stays silent,
    because there IS nothing to report.
    """
    graph, sor, *_ = _path_fixture()
    missing = await _run(
        graph, sor, GraphQueryParams(template="path", entity="nobody", other_entity="c", hops=3)
    )
    assert missing.results == ()
    assert _codes(missing) == ["GUARDRAIL_BLOCKED"]
    # the message must name WHICH string failed and where to check it —
    # a bare "not found" leaves the agent with the same dead end
    assert "'nobody'" in missing.warnings[0].message
    assert "get_entity" in missing.warnings[0].message

    # the resolvable side must not be blamed
    assert "'c'" not in missing.warnings[0].message

    graph_no_path = _FakeGraph(path=None)
    none_found = await _run(
        graph_no_path, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert none_found.results == () and none_found.warnings == ()


async def test_both_unresolved_path_endpoints_are_each_named() -> None:
    """Naming only the first failure would send the agent to fix one name and
    hit the identical wall on the second."""
    graph, sor, *_ = _path_fixture()
    response = await _run(
        graph,
        sor,
        GraphQueryParams(template="path", entity="nobody", other_entity="nor-me", hops=3),
    )
    assert response.results == ()
    assert _codes(response) == ["GUARDRAIL_BLOCKED", "GUARDRAIL_BLOCKED"]
    assert "'nobody'" in response.warnings[0].message
    assert "'nor-me'" in response.warnings[1].message


async def test_an_unresolved_neighbors_seed_warns_instead_of_returning_a_silent_empty() -> None:
    """The neighbors/subgraph twin of the path case — same trap, same recovery."""
    for template in ("neighbors", "subgraph"):
        response = await _run(
            _FakeGraph(),
            _FakeSoR(seeds={}),
            GraphQueryParams(template=template, entity="ZZZ_no_such_entity", hops=1),
        )
        assert response.results == (), template
        assert _codes(response) == ["GUARDRAIL_BLOCKED"], template
        assert "'ZZZ_no_such_entity'" in response.warnings[0].message, template


# -- subgraph ----------------------------------------------------------------------


async def test_subgraph_emits_cited_entities_and_evidence_backed_relations() -> None:
    seed, other = uuid.uuid4(), uuid.uuid4()
    rel_id = uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[{"entity": _node(other, "Other"), "distance": 1}],
        edges=[{"src": str(seed), "dst": str(other), "type": "works_at"}],
    )
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={seed: [("text", "c-seed")], other: [("text", "c-other")]},
        relations={
            (seed, other, "works_at"): (
                rel_id,
                [
                    _chunk_evidence(),
                    {"evidence_type": "row", "evidence_ref": "6:orders:17"},
                    {
                        "evidence_type": "manual",
                        "evidence_ref": "m-1",
                        "quote": "hand-checked",
                        "source_uri": "file:///note",
                    },
                ],
            )
        },
    )
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme"))
    by_type = {r.result_type for r in response.results}
    assert by_type == {"entity", "relation"}
    relation = next(r for r in response.results if r.result_type == "relation")
    assert relation.id == str(rel_id)
    kinds = sorted(ref.source_type for ref in relation.source_refs)
    assert kinds == ["chunk", "document", "row"]  # all three evidence shapes emitted
    row_ref = next(ref for ref in relation.source_refs if ref.source_type == "row")
    assert row_ref.metadata == {"table": "orders", "pk": "17"}  # split losslessly
    # the relation renders as a one-hop path with SoR names — visible text
    # for agents and §20 relation_hit_rate (a reverted emission fails here;
    # the fake's active_entity_names returns name-{id})
    assert relation.title == f"name-{seed} -[works_at]-> name-{other}"
    # the SEED entity is part of the subgraph (unlike plain neighbors)
    assert {r.id for r in response.results if r.result_type == "entity"} == {
        str(seed),
        str(other),
    }
    assert response.warnings == ()


async def test_subgraph_caps_the_combined_response_at_max_rows() -> None:
    """§21: max_rows ceils the WHOLE response — entities AND relations. A dense
    neighborhood has O(n²) edges, so without a combined cap a max_rows policy
    would still return dozens of relation rows (and fetch unbounded edges).
    Entities keep priority; relations fill the remainder; the clip is TRUNCATED."""
    seed = uuid.uuid4()
    others = [uuid.uuid4() for _ in range(2)]  # 3 entities incl. seed
    graph = _FakeGraph(
        neighbor_rows=[{"entity": _node(o), "distance": 1} for o in others],
        edges=[{"src": str(seed), "dst": str(others[0]), "type": f"t{i}"} for i in range(9)],
    )
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={eid: [("text", f"c-{eid}")] for eid in [seed, *others]},
        relations={
            (seed, others[0], f"t{i}"): (uuid.uuid4(), [_chunk_evidence()]) for i in range(9)
        },
        pairs={(seed, others[0]), (seed, others[1])},
    )
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme"))
    assert len(response.results) == _POLICY.max_rows  # 3 entities + 7 relations = the ceiling
    assert sum(1 for r in response.results if r.result_type == "entity") == 3
    assert sum(1 for r in response.results if r.result_type == "relation") == 7
    assert "TRUNCATED" in _codes(response)
    edge_call = next(c for c in graph.calls if c[0] == "edges_among")
    assert edge_call[1]["limit"] == _POLICY.max_rows - 3 + 1  # the fetch is capped too (probe)


async def test_subgraph_with_no_edge_budget_skips_the_edge_query_and_flags() -> None:
    """Entities alone can fill the ceiling; ≥2 connected-by-construction nodes
    mean edges exist that had no room — surfaced as TRUNCATED, and the edge
    query is not even sent (its results could never be emitted)."""
    seed = uuid.uuid4()
    others = [uuid.uuid4() for _ in range(_POLICY.max_rows - 1)]  # fills the cap with seed
    graph = _FakeGraph(neighbor_rows=[{"entity": _node(o), "distance": 1} for o in others])
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={eid: [("text", f"c-{eid}")] for eid in [seed, *others]},
        pairs={(seed, o) for o in others},
    )
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme"))
    assert len(response.results) == _POLICY.max_rows
    assert all(r.result_type == "entity" for r in response.results)
    assert "TRUNCATED" in _codes(response)
    assert not any(c[0] == "edges_among" for c in graph.calls)  # no budget → no query


async def test_no_budget_at_multi_hop_probes_for_edges_instead_of_asserting() -> None:
    """At hops ≥ 2 a neighbor can connect solely through an EXCLUDED
    intermediate, so direct edges among the kept nodes may genuinely not exist
    — TRUNCATED must come from a LIMIT-1 existence probe, not an assertion:
    over-firing is a spurious warning, under-firing is a silent omission."""
    seed = uuid.uuid4()
    others = [uuid.uuid4() for _ in range(_POLICY.max_rows - 1)]
    mentions = {eid: [("text", f"c-{eid}")] for eid in [seed, *others]}
    rows = [{"entity": _node(o), "distance": 2} for o in others]
    pairs = {(seed, o) for o in others}

    # no direct edges exist → the probe finds nothing → NOT truncated
    graph = _FakeGraph(neighbor_rows=rows)
    sor = _FakeSoR(seeds={"acme": [seed]}, mentions=mentions, pairs=pairs)
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme", hops=2))
    assert "TRUNCATED" not in _codes(response)
    probe = next(c for c in graph.calls if c[0] == "edges_among")
    assert probe[1]["limit"] == 1  # an existence probe, not a fetch

    # a direct edge exists → the probe finds it → TRUNCATED
    graph = _FakeGraph(
        neighbor_rows=rows,
        edges=[{"src": str(seed), "dst": str(others[0]), "type": "t"}],
    )
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme", hops=2))
    assert "TRUNCATED" in _codes(response)


async def test_a_relation_whose_evidence_cannot_satisfy_the_contract_is_dropped() -> None:
    """Nullable SoR columns vs frozen contract shapes: evidence rows that lack
    a required field (blank quote, missing uri, unsplittable row ref) are
    skipped; a relation left with ZERO citable evidence is dropped + counted
    (§27.2 relation → ≥1 evidence)."""
    seed, other = uuid.uuid4(), uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[{"entity": _node(other), "distance": 1}],
        edges=[{"src": str(seed), "dst": str(other), "type": "works_at"}],
    )
    bad_rows: list[dict[str, Any]] = [
        _chunk_evidence(quote=None),  # chunk without its quote
        _chunk_evidence(source_uri=""),  # chunk without a uri
        _chunk_evidence(start_offset=None),  # chunk without its span
        {"evidence_type": "row", "evidence_ref": "corrupt"},  # unsplittable row ref
        {"evidence_type": "manual", "evidence_ref": "m", "quote": "", "source_uri": "u"},
        {"evidence_type": "alien", "evidence_ref": "x"},  # out-of-vocabulary type
    ]
    sor = _FakeSoR(
        seeds={"acme": [seed]},
        mentions={seed: [("text", "c-seed")], other: [("text", "c-other")]},
        relations={(seed, other, "works_at"): (uuid.uuid4(), bad_rows)},
    )
    response = await _run(graph, sor, GraphQueryParams(template="subgraph", entity="acme"))
    assert {r.result_type for r in response.results} == {"entity"}  # relation dropped
    assert _codes(response) == ["PARTIAL_RESULTS"]


# -- degradation (§22: typed, never a 500) ------------------------------------------


class _TimedOutError(ClientError):
    """ClientError.code is a read-only property fed by the server response;
    the subclass attribute shadows it with the timeout code under test."""

    code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"


async def test_a_traversal_timeout_degrades_to_partial_results() -> None:
    graph = _FakeGraph(raise_exc=_TimedOutError("timed out"))
    sor = _FakeSoR(seeds={"acme": [uuid.uuid4()]})
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert response.results == () and _codes(response) == ["PARTIAL_RESULTS"]
    assert "deadline" in response.warnings[0].message


async def test_an_unavailable_store_degrades_to_store_unavailable() -> None:
    graph = _FakeGraph(raise_exc=ServiceUnavailable("connection refused"))
    sor = _FakeSoR(seeds={"acme": [uuid.uuid4()]})
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert response.results == () and _codes(response) == ["STORE_UNAVAILABLE"]


# -- subgraph_context (BA3c: the id-seeded REST GraphContext producer) ---------


class _SubgraphSoR(_FakeSoR):
    """_FakeSoR + the emission fetches. Shape-only fakes (rows are canned, the
    in_ predicates are not re-evaluated here) — the live SQL filtering is the
    integration suite's job; these pin the ORCHESTRATION: which ids are asked
    for, what gets emitted, in what shape and order."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.entity_rows: list[Any] = kwargs.pop("entity_rows", [])
        self.relation_rows: list[Any] = kwargs.pop("relation_rows", [])
        super().__init__(*args, **kwargs)

    async def fetch_all(self, table: Any, *where: Any) -> list[Any]:
        if table.name == "entities":
            return self.entity_rows
        if table.name == "relations":
            return self.relation_rows
        raise AssertionError(f"unexpected emission fetch on {table.name}")


def _sor_entity_row(entity_id: uuid.UUID, name: str) -> Any:
    return SimpleNamespace(id=entity_id, type="Person", canonical_name=name)


def _sor_relation_row(rel_id: uuid.UUID, src: uuid.UUID, dst: uuid.UUID) -> Any:
    return SimpleNamespace(
        id=rel_id, src_entity_id=src, dst_entity_id=dst, type="WORKS_AT", confidence=0.9
    )


async def _run_subgraph(
    graph: _FakeGraph, sor: _SubgraphSoR, seed: uuid.UUID, hops: int, max_hops: int = 3
) -> Any:
    return await subgraph_context(
        cast(BuildScopedGraphRepo, graph),
        cast(BuildScopedRepo, sor),
        _POLICY,
        seed,
        hops,
        max_graph_hops=max_hops,
    )


async def test_subgraph_context_rejects_scope_mismatch_and_ceiling() -> None:
    graph = _FakeGraph()
    sor = _SubgraphSoR()
    graph.build_id = uuid.uuid4()  # not the SoR's build
    with pytest.raises(ValueError, match="different scopes"):
        await _run_subgraph(graph, sor, uuid.uuid4(), 1)
    graph.build_id = _BUILD
    with pytest.raises(ValueError, match="rejected, not clamped"):
        await _run_subgraph(graph, sor, uuid.uuid4(), 4)


async def test_subgraph_context_inactive_seed_is_none() -> None:
    # WHY: the endpoint's 404 — a merged/rejected/unknown entity is invisible
    # on every surface (C6 doctrine), so an id-seeded subgraph of one must be
    # a lookup miss, never an empty-but-200 context.
    seed = uuid.uuid4()
    sor = _SubgraphSoR(active=set())  # seed not active
    assert (await _run_subgraph(_FakeGraph(), sor, seed, 1)) is None


async def test_subgraph_context_emits_sor_backed_nodes_and_citable_edges() -> None:
    # WHY (#31/#33): the projection contributes topology only — every emitted
    # node/edge value comes from the SoR; a mention-less entity and an
    # evidence-less edge are invisible here exactly as they are on the MCP
    # surface (one citability bar, no REST side-channel).
    seed, buddy, silent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rel = uuid.uuid4()
    graph = _FakeGraph(
        neighbor_rows=[
            {"entity": _node(buddy), "distance": 1},
            {"entity": _node(silent), "distance": 1},
        ],
        edges=[
            {"src": str(seed), "dst": str(buddy), "type": "WORKS_AT"},  # citable
            {"src": str(seed), "dst": str(silent), "type": "KNOWS"},  # dst dropped
        ],
    )
    sor = _SubgraphSoR(
        mentions={
            seed: [("structured", "t:1")],
            buddy: [("structured", "t:2")],
            # silent: no mentions → dropped from the node set (§16)
        },
        relations={(seed, buddy, "WORKS_AT"): (rel, [_chunk_evidence()])},
        pairs={(seed, buddy), (seed, silent)},
        entity_rows=[_sor_entity_row(seed, "Alice"), _sor_entity_row(buddy, "Acme")],
        relation_rows=[_sor_relation_row(rel, seed, buddy)],
    )
    context = await _run_subgraph(graph, sor, seed, 1)
    assert context is not None
    assert [n["id"] for n in context.nodes] == [seed, buddy]  # nearest-first, seed at 0
    assert context.nodes[0] == {"id": seed, "type": "Person", "label": "Alice"}
    (edge,) = context.edges
    assert edge == {
        "id": rel,
        "src": seed,
        "dst": buddy,
        "type": "WORKS_AT",
        "confidence": 0.9,
    }
