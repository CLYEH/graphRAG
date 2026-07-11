"""Why: BA6a's routers own the REST half of the playground — the frozen
QueryResult reprojection of the §16 dict (mode/build_id/results/graph_context/
warnings/debug; the nil-uuid binding sentinel stays in data but never leaks
into meta's nullable build_id), the registry-policy gates (BA3c seam), the
unsupported filters/options loud-reject, and the typed 503 for an
unconfigured model. The deadline/binding/degradation machinery itself is
core's run_bounded_query (shared with MCP — tested there); these tests stub
it and pin the HTTP orchestration.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.app import create_app
from core.llm.factory import LLMNotConfiguredError
from core.stores.repo import NoActiveBuildError

pytestmark = pytest.mark.contract

_BUILD = str(uuid.uuid4())
_NIL = "00000000-0000-0000-0000-000000000000"

_QUERY_POLICY: dict[str, Any] = {
    "schema_version": "1.0",
    "default_mode": "hybrid",
    "max_top_k": 20,
    "max_graph_hops": 3,
    "max_sql_rows": 100,
    "max_latency_ms": 10000,
    "require_sources": True,
    "expose_debug": True,
    "text_to_sql": {
        "enabled": False,
        "readonly": True,
        "allowed_tables": [],
        "blocked_keywords": ["insert", "update", "delete", "drop", "alter", "truncate"],
        "max_rows": 100,
        "timeout_ms": 5000,
    },
    "text_to_cypher": {
        "enabled": False,
        "readonly": True,
        "allowed_clauses": ["MATCH", "WHERE", "RETURN", "LIMIT"],
        "blocked": ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
        "max_rows": 100,
        "timeout_ms": 5000,
    },
}


class _FakeEngine:
    """Counts checkouts so tests can pin the P1 property: the policy
    connection is RETURNED before the bounded query runs (a request must
    never hold one pool connection while acquiring another — Codex #60 R2)."""

    def __init__(self) -> None:
        self.open = 0

    def connect(self) -> Any:
        engine = self

        class _Conn:
            async def __aenter__(self) -> object:
                engine.open += 1
                return object()

            async def __aexit__(self, *exc: Any) -> None:
                engine.open -= 1

        return _Conn()


@pytest.fixture()
def fake_engine() -> _FakeEngine:
    return _FakeEngine()


@pytest.fixture()
def client(fake_engine: _FakeEngine) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as c:
        # the query endpoints read policy on short-lived connections off the
        # app engine — swap in the counting fake AFTER lifespan set the real
        # (lazy, unconnected) one
        cast("FastAPI", c.app).state.engine = fake_engine
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.query.{name}", fn)


def _queryable(monkeypatch: pytest.MonkeyPatch, *, config: dict[str, Any] | None = None) -> None:
    project_config = {"query_policy": _QUERY_POLICY} if config is None else config

    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=project_config)

    async def fake_binding(conn: Any, name: str) -> Any:
        return SimpleNamespace(build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "resolve_active_binding", fake_binding)
    _stub(monkeypatch, "project_query_context", lambda request, project: object())


def _mcp_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "query": "q",
        "tool": "query_semantic",
        "project": "p",
        "build_id": _BUILD,
        "results": [{"result_type": "chunk", "id": str(uuid.uuid4())}],
        "graph_context": None,
        "warnings": [],
        "debug": None,
    }
    base.update(over)
    return base


def test_reprojection_onto_the_frozen_query_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_engine: _FakeEngine
) -> None:
    # WHY: the REST payload is the §16 dict REPROJECTED — mode from the
    # endpoint, tool/project/query/schema_version dropped, the rest verbatim;
    # meta.build_id names the bound build.
    _queryable(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_bounded(context: Any, policy: Any, tool: str, query: str, runner: Any) -> Any:
        captured.update({"tool": tool, "query": query, "max_latency": policy.max_latency_ms})
        captured["held_connections"] = fake_engine.open
        return _mcp_dict()

    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    r = client.post("/projects/p/query/semantic", json={"query": "who is alice"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {"mode", "build_id", "results", "graph_context", "warnings", "debug"}
    assert data["mode"] == "semantic" and data["build_id"] == _BUILD
    assert "tool" not in data and "project" not in data  # §16-only keys dropped
    assert r.json()["meta"]["build_id"] == _BUILD
    assert captured["tool"] == "query_semantic" and captured["query"] == "who is alice"
    assert captured["max_latency"] == 10000  # the registry policy reached the seam
    # the P1 pin (Codex #60 R2): by the time the bounded query runs, the
    # policy-read connection is BACK in the pool — zero held checkouts
    assert captured["held_connections"] == 0


def test_top_k_is_accepted_and_reaches_the_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the over-block dual of the reject matrix: a legal top_k must be a 200
    # AND actually thread body.top_k → policy.top_k() → the mode call — so
    # the fake seam DRIVES the real runner closure and a stubbed run_semantic
    # records the k it received (a policy-method-only check would be a
    # false-green for the router wiring)
    _queryable(monkeypatch)
    captured_k: list[int] = []

    async def fake_run_semantic(repo: Any, vectors: Any, embedder: Any, q: Any, k: int) -> Any:
        captured_k.append(k)
        return _mcp_dict()

    async def fake_bounded(context: Any, policy: Any, tool: str, query: str, runner: Any) -> Any:
        deps = SimpleNamespace(repo=None, vectors=None, embedder=None)
        return await runner(deps, 1000)

    _stub(monkeypatch, "run_semantic", fake_run_semantic)
    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    assert (
        client.post("/projects/p/query/semantic", json={"query": "q", "top_k": 5}).status_code
        == 200
    )
    assert (
        client.post("/projects/p/query/semantic", json={"query": "q", "top_k": 500}).status_code
        == 200
    )
    assert captured_k == [5, 20]  # 5 passes un-clamped; 500 clamps to max_top_k

    # the sql sibling (Codex #60 R1+R4): top_k clamps through max_top_k
    # FIRST (the frozen schema's request-level cap), then meets the sql row
    # ceiling — never ignored, never widening past EITHER bound
    captured_rows: list[int] = []

    async def fake_run_sql(reader: Any, llm: Any, policy: Any, q: Any, rows: int) -> Any:
        captured_rows.append(rows)
        return _mcp_dict()

    async def fake_bounded_sql(
        context: Any, policy: Any, tool: str, query: str, runner: Any
    ) -> Any:
        deps = SimpleNamespace(sql_reader=None, llm=None)
        return await runner(deps, 1000)

    _stub(monkeypatch, "run_sql", fake_run_sql)
    _stub(monkeypatch, "run_bounded_query", fake_bounded_sql)
    assert client.post("/projects/p/query/sql", json={"query": "q", "top_k": 1}).status_code == 200
    assert (
        client.post("/projects/p/query/sql", json={"query": "q", "top_k": 999}).status_code == 200
    )
    assert client.post("/projects/p/query/sql", json={"query": "q"}).status_code == 200
    # 1 passes; 999 → max_top_k (20), NOT just sql_rows (100); absent → ceiling
    assert captured_rows == [1, 20, 100]


def test_nil_build_sentinel_stays_in_data_never_meta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (MCP precedent): a deadline firing DURING binding leaves no real
    # build — data.build_id keeps the format-legal nil sentinel (required
    # field), but meta.build_id is nullable and must not present nil as real.
    _queryable(monkeypatch)

    async def fake_bounded(*args: Any, **kwargs: Any) -> Any:
        return _mcp_dict(build_id=_NIL, results=[], warnings=[{"code": "PARTIAL_RESULTS"}])

    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    r = client.post("/projects/p/query/global", json={"query": "overview"})
    assert r.json()["data"]["build_id"] == _NIL
    assert r.json()["meta"]["build_id"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q", "filters": {"type": "Person"}},
        {"query": "q", "options": {"max_hops": 2}},
        {"query": ""},  # minLength 1
        {"query": "q", "top_k": 0},  # ge 1
    ],
)
def test_unsupported_or_invalid_bodies_reject_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    _queryable(monkeypatch)

    async def fail_bounded(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    _stub(monkeypatch, "run_bounded_query", fail_bounded)
    r = client.post("/projects/p/query/sql", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_policy_gates_and_typed_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # policy missing → 400 (BA3c precedent, strict — no invented defaults)
    _queryable(monkeypatch, config={})
    r = client.post("/projects/p/query/semantic", json={"query": "q"})
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"query_policy": "missing"}

    # unknown project → 404
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "get_project", missing)
    r = client.post("/projects/ghost/query/global", json={"query": "q"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    # unconfigured model → typed 503, never a coarse 500 (GAP note)
    _queryable(monkeypatch)

    def no_key(request: Any, project: str) -> Any:
        raise LLMNotConfiguredError("OPENAI_API_KEY is not set")

    _stub(monkeypatch, "project_query_context", no_key)
    r = client.post("/projects/p/query/semantic", json={"query": "q"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "STORE_UNAVAILABLE"
    assert "OPENAI_API_KEY" in r.json()["error"]["message"]

    # no active build (raised inside the seam's binding) → 409
    _queryable(monkeypatch)

    async def no_active(*args: Any, **kwargs: Any) -> Any:
        raise NoActiveBuildError("p")

    _stub(monkeypatch, "run_bounded_query", no_active)
    r = client.post("/projects/p/query/global", json={"query": "q"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"


def test_graph_options_thread_to_the_real_runner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (BA6b): options is the contract's mode-specific channel — the typed
    # vocabulary must reach GraphQueryParams field-for-field, and the §21
    # max_graph_hops ceiling must reach run_graph (fake seam drives the REAL
    # runner closure; a params-construction-only check would be false-green
    # for the router wiring, the BA6a top_k lesson)
    _queryable(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_run_graph(
        graph: Any, repo: Any, cypher: Any, params: Any, query: str, max_hops: int
    ) -> Any:
        captured.update({"params": params, "query": query, "max_hops": max_hops})
        return _mcp_dict(tool="graph_query")

    async def fake_bounded(context: Any, policy: Any, tool: str, query: str, runner: Any) -> Any:
        captured["tool"] = tool
        return await runner(SimpleNamespace(graph=None, repo=None), 1000)

    _stub(monkeypatch, "run_graph", fake_run_graph)
    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    body = {
        "query": "who neighbors alice",
        "options": {"template": "neighbors", "entity": "alice", "hops": 2},
    }
    r = client.post("/projects/p/query/graph", json=body)
    assert r.status_code == 200
    assert r.json()["data"]["mode"] == "graph"
    p = captured["params"]
    assert (p.template, p.entity, p.other_entity, p.hops) == ("neighbors", "alice", None, 2)
    assert captured["max_hops"] == 3  # the §21 ceiling reached the runner
    assert captured["tool"] == "query_graph" and captured["query"] == "who neighbors alice"


def test_graph_guardrail_stays_in_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (facade parity): template vocabulary and the hop ceiling are CORE's
    # guardrail — an unknown template degrades typed IN-ENVELOPE (200 +
    # GUARDRAIL_BLOCKED, rejected-not-clamped) exactly as the MCP graph tool
    # answers, never a REST-only 400. Drives the REAL run_graph; the fake
    # deps are never touched (blocked before any store I/O).
    _queryable(monkeypatch)
    scope = SimpleNamespace(project="p", build_id=uuid.uuid4())

    async def fake_bounded(context: Any, policy: Any, tool: str, query: str, runner: Any) -> Any:
        response = await runner(SimpleNamespace(graph=scope, repo=scope), 1000)
        return response.to_dict()

    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    for body in (
        {"query": "q", "options": {"template": "teleport", "entity": "alice"}},
        {"query": "q", "options": {"template": "neighbors", "entity": "alice", "hops": 99}},
        # blankness is a VALUE question too — MCP's tool schema passes an
        # empty string through to the same core guardrail (facade parity).
        # EMPTY strings exactly: a min_length=1 regression would 400 these
        # (whitespace would slip past it — non-discriminating)
        {"query": "q", "options": {"template": "neighbors", "entity": ""}},
        {"query": "q", "options": {"template": "", "entity": "alice"}},
    ):
        r = client.post("/projects/p/query/graph", json=body)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["results"] == []
        assert [w["code"] for w in data["warnings"]] == ["GUARDRAIL_BLOCKED"]


def test_hybrid_threads_remaining_budget_top_k_and_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (the C8 class-11 face): hybrid's pacer must start from what binding
    # LEFT of the §21 budget — the seam's remaining_ms, never a fresh full
    # max_latency_ms. The 1234 sentinel discriminates: a fresh-budget
    # regression would show 10000. top_k clamps through max_top_k (the cap
    # chain), and options → GraphQueryParams / absent → None (skip-with-reason
    # parity is core's, tested there).
    _queryable(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_run_hybrid(deps: Any, hpolicy: Any, query: str, params: Any) -> Any:
        captured.update(
            {"latency": hpolicy.max_latency_ms, "top_k": hpolicy.top_k, "params": params}
        )
        return _mcp_dict(tool="hybrid_query")

    async def fake_bounded(context: Any, policy: Any, tool: str, query: str, runner: Any) -> Any:
        captured["tool"] = tool
        return await runner(SimpleNamespace(), 1234)

    _stub(monkeypatch, "run_hybrid", fake_run_hybrid)
    _stub(monkeypatch, "run_bounded_query", fake_bounded)
    r = client.post("/projects/p/query/hybrid", json={"query": "q", "top_k": 500})
    assert r.status_code == 200
    assert r.json()["data"]["mode"] == "hybrid"
    assert captured["latency"] == 1234  # binding's remainder, not a fresh budget
    assert captured["top_k"] == 20  # clamped through max_top_k
    assert captured["params"] is None  # no options → router skips graph with a reason
    assert captured["tool"] == "query_hybrid"

    body = {"query": "q", "options": {"template": "path", "entity": "a", "other_entity": "b"}}
    assert client.post("/projects/p/query/hybrid", json=body).status_code == 200
    p: Any = captured["params"]
    assert (p.template, p.entity, p.other_entity, p.hops) == ("path", "a", "b", 1)


@pytest.mark.parametrize(
    ("mode", "body"),
    [
        ("graph", {"query": "q"}),  # options REQUIRED for graph
        ("graph", {"query": "q", "options": {"template": "neighbors"}}),  # entity required
        ("graph", {"query": "q", "options": {"template": "n", "entity": "a", "beam": 3}}),
        ("graph", {"query": "q", "top_k": 5, "options": {"template": "n", "entity": "a"}}),
        ("graph", {"query": "q", "filters": {"t": 1}, "options": {"template": "n", "entity": "a"}}),
        ("hybrid", {"query": "q", "options": {"entity": "a"}}),  # partial options
        ("hybrid", {"query": "q", "options": {"template": "n", "entity": "a", "beam": 3}}),
        ("hybrid", {"query": "q", "filters": {"t": 1}}),
        # explicit null ≠ omission (the contract's options is an optional
        # OBJECT, not nullable) — folding null into skip-graph would hide a
        # malformed request behind incomplete hybrid results (Codex #61 R1)
        ("hybrid", {"query": "q", "options": None}),
    ],
)
def test_graph_hybrid_shape_rejections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str, body: dict[str, Any]
) -> None:
    # WHY: the options channel is a VALIDATED vocabulary — unknown keys,
    # partial invocations, and per-mode unsupported fields (graph top_k: the
    # MCP tool exposes no per-call cap; accepting-and-ignoring is the #60 R1
    # lie) all reject loudly at the shape layer, the same layer as the MCP
    # tool's typed parameters.
    _queryable(monkeypatch)

    async def fail_bounded(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    _stub(monkeypatch, "run_bounded_query", fail_bounded)
    r = client.post(f"/projects/p/query/{mode}", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_no_active_build_precedes_the_config_gates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (Codex #60 R3 — the inspect _bind precedence): a bootstrap project
    # with no active build must hear the frozen 409 NO_ACTIVE_BUILD even when
    # its config is ALSO missing the policy (and would 400) — config errors
    # would send the client to fix settings when there is nothing to query.
    # Discriminating: with the old gate order this request returned 400.
    _queryable(monkeypatch, config={})  # policy missing too

    async def no_build(conn: Any, name: str) -> Any:
        raise NoActiveBuildError("p")

    _stub(monkeypatch, "resolve_active_binding", no_build)
    r = client.post("/projects/p/query/semantic", json={"query": "q"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"


def test_policy_store_outage_is_a_typed_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (Codex #60 R4 — the inspect Neo4j precedent): the preflight reads
    # run BEFORE the seam's §22 degradation path, so a Postgres/pool outage
    # there must be the typed 503 STORE_UNAVAILABLE itself — never the
    # generic INTERNAL 500, which tells the client "server bug" when the
    # store is merely down.
    _queryable(monkeypatch)

    async def pg_down(conn: Any, name: str) -> Any:
        raise OperationalError("select 1", None, OSError("connection refused"))

    _stub(monkeypatch, "get_project", pg_down)
    r = client.post("/projects/p/query/semantic", json={"query": "q"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "STORE_UNAVAILABLE"
