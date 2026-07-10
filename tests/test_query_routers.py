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

    _stub(monkeypatch, "get_project", fake_get_project)
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

    # the sql sibling (Codex #60 R1): top_k NARROWS the §21 row ceiling —
    # min(top_k, sql_rows()) — never ignored, never widening
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
    assert captured_rows == [1, 100, 100]  # narrows; clamps to sql_rows(); defaults to it


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
