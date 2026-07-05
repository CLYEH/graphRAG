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

import json
import uuid
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable

from core.query.graph import GraphQueryParams, graph_query
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
    ) -> None:
        self.project = _PROJECT
        self.build_id = _BUILD
        self._neighbors = neighbor_rows or []
        self._path = path
        self._edges = edges or []
        self._raise = raise_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def neighbors(
        self, seed: str, *, hops: int, limit: int, timeout_ms: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("neighbors", {"seed": seed, "hops": hops, "limit": limit}))
        if self._raise is not None:
            raise self._raise
        return self._neighbors

    async def shortest_path(
        self, src: str, dst: str, *, max_hops: int, timeout_ms: int
    ) -> dict[str, Any] | None:
        self.calls.append(("shortest_path", {"src": src, "dst": dst, "max_hops": max_hops}))
        if self._raise is not None:
            raise self._raise
        return self._path

    async def edges_among(
        self, canonical_ids: list[str], *, timeout_ms: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("edges_among", {"ids": list(canonical_ids)}))
        return self._edges


class _FakeSoR:
    """The Postgres side: seeds, mentions, active-status, relations+evidence."""

    def __init__(
        self,
        seeds: dict[str, list[uuid.UUID]] | None = None,
        mentions: dict[uuid.UUID, list[tuple[str, str]]] | None = None,
        active: set[uuid.UUID] | None = None,
        relations: dict[tuple[uuid.UUID, uuid.UUID, str], tuple[uuid.UUID, list[dict[str, Any]]]]
        | None = None,
    ) -> None:
        self.project = _PROJECT
        self.build_id = _BUILD
        self._seeds = seeds or {}
        self._mentions = mentions or {}
        self._active = active
        self._relations = relations or {}

    async def entity_ids_by_name(self, name: str) -> list[uuid.UUID]:
        return self._seeds.get(name.lower(), [])

    async def mentions_by_entity(
        self, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, str]]]:
        return {eid: refs for eid, refs in self._mentions.items() if eid in entity_ids}

    async def active_entity_ids(self, entity_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if self._active is not None:
            return {eid for eid in entity_ids if eid in self._active}
        return set(entity_ids)  # default: everything still active

    async def relations_with_evidence(
        self, triples: list[tuple[uuid.UUID, uuid.UUID, str]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID, str], tuple[uuid.UUID, list[dict[str, Any]]]]:
        return {t: self._relations[t] for t in triples if t in self._relations}


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
    )
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert response.warnings == ()
    assert [r.id for r in response.results] == [str(near), str(far)]  # nearest first
    assert response.results[0].source_refs[0].source_type == "chunk"
    assert response.results[1].source_refs[0].source_type == "row"
    assert response.results[0].result_type == "entity"


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
    sor = _FakeSoR(seeds={"acme": [seed]}, mentions={ok: [("text", "c-1")]})
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
    sor = _FakeSoR(seeds={"acme": [seed]}, mentions={ok: [("text", "c-1")]})
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert [r.id for r in response.results] == [str(ok)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_the_policy_row_cap_truncates_and_flags() -> None:
    """max_rows+1 rows come back (the probe) → clipped to max_rows and flagged
    TRUNCATED (§22) — the §21 policy ceiling, not a caller choice."""
    seed = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(_POLICY.max_rows + 1)]
    graph = _FakeGraph(neighbor_rows=[{"entity": _node(eid), "distance": 1} for eid in ids])
    sor = _FakeSoR(seeds={"acme": [seed]}, mentions={eid: [("text", f"c-{eid}")] for eid in ids})
    response = await _run(graph, sor, GraphQueryParams(template="neighbors", entity="acme"))
    assert len(response.results) == _POLICY.max_rows
    assert _codes(response) == ["TRUNCATED"]


async def test_an_unknown_seed_yields_an_empty_result_not_an_error() -> None:
    graph = _FakeGraph()
    response = await _run(graph, _FakeSoR(), GraphQueryParams(template="neighbors", entity="acme"))
    assert response.results == () and response.warnings == ()
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


async def test_no_seeds_or_no_path_yield_empty_without_warnings() -> None:
    graph, sor, *_ = _path_fixture()
    missing = await _run(
        graph, sor, GraphQueryParams(template="path", entity="nobody", other_entity="c", hops=3)
    )
    assert missing.results == () and missing.warnings == ()

    graph_no_path = _FakeGraph(path=None)
    none_found = await _run(
        graph_no_path, sor, GraphQueryParams(template="path", entity="a", other_entity="c", hops=3)
    )
    assert none_found.results == () and none_found.warnings == ()


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
    # the SEED entity is part of the subgraph (unlike plain neighbors)
    assert {r.id for r in response.results if r.result_type == "entity"} == {
        str(seed),
        str(other),
    }
    assert response.warnings == ()


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
