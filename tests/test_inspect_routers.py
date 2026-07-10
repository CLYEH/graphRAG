"""Why: BA3a's routers own the HTTP behaviors above the DR-006 repo — the
project-404-before-binding-409 ordering, the §15 envelope with meta.build_id
stamped from the ACTIVE binding (the API's first active-build consumer), the
opaque keyset cursors, the contract-licensed `raw`-on-detail-only key, and the
not-found GAP mapping (true 404 status + coarse frozen code — the enum has no
inspect not-found code yet). These hold without Postgres: the binding and repo
are stubbed; the live SQL/scope behavior is the integration suite's job.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from api.pagination import decode_chunk_cursor, decode_id_cursor, encode_cursor
from core.stores.repo import NoActiveBuildError

pytestmark = pytest.mark.contract

_TS = datetime(2026, 7, 10, tzinfo=UTC)
_BUILD = uuid.uuid4()


def _doc_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "source_uri": "file:///d.txt",
        "raw": "full text",
        "content_hash": "h1",
        "mime": "text/plain",
        "metadata": None,
        "status": "ingested",
        "ingested_at": _TS,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _chunk_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "build_id": _BUILD,
        "ordinal": 0,
        "text": "chunk text",
        "token_count": None,
        "start_offset": 0,
        "end_offset": 10,
        "vector_point_id": None,
        "metadata": {"k": 1},
        "status": "embedded",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()  # binding + repo are stubbed; the connection is never used

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.inspect.{name}", fn)


def _bindable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Project exists and has an active build (the happy binding)."""

    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)


class _FakeRepo:
    """Captures fetch_page/fetch_all args and serves scripted rows."""

    pages: Sequence[Any] = ()
    rows: Sequence[Any] = ()
    calls: list[dict[str, Any]] = []

    @classmethod
    def bound_to(cls, conn: Any, binding: Any) -> _FakeRepo:
        return cls()

    async def fetch_page(self, table: Any, *where: Any, order_by: Any, limit: int) -> Sequence[Any]:
        type(self).calls.append({"where": where, "order_by": order_by, "limit": limit})
        return type(self).pages

    async def fetch_all(self, table: Any, *where: Any) -> Sequence[Any]:
        return type(self).rows


@pytest.fixture()
def repo(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRepo]:
    _FakeRepo.pages, _FakeRepo.rows, _FakeRepo.calls = (), (), []
    _stub(monkeypatch, "BuildScopedRepo", _FakeRepo)
    return _FakeRepo


def test_list_documents_stamps_the_binding_and_omits_raw(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: meta.build_id is §15's "which build served this" — the inspect
    # surface is the API's first consumer of the active binding; and the
    # contract licenses `raw` on detail GET only, so a list frame must not
    # even carry the key.
    _bindable(monkeypatch)
    repo.pages = (_doc_row(), _doc_row())

    r = client.get("/projects/p/documents")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["build_id"] == str(_BUILD)
    assert body["meta"]["next_cursor"] is None  # short page → last page
    assert len(body["data"]) == 2
    for doc in body["data"]:
        assert "raw" not in doc  # detail-only key, absent on list
        assert doc["metadata"] == {}  # DB NULL coalesces to the empty object


def test_list_documents_pagination_cursor_round_trips(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    rows = [_doc_row() for _ in range(3)]
    repo.pages = rows  # limit+1 rows → a next page exists

    r = client.get("/projects/p/documents", params={"limit": 2})
    body = r.json()
    assert [d["id"] for d in body["data"]] == [str(rows[0].id), str(rows[1].id)]
    token = body["meta"]["next_cursor"]
    assert decode_id_cursor(token) == (rows[1].id,)  # last IN-PAGE row, not the probe
    assert repo.calls[0]["limit"] == 3  # limit+1 probe

    client.get("/projects/p/documents", params={"limit": 2, "cursor": token})
    assert len(repo.calls[1]["where"]) == 1  # the keyset predicate rode along


def test_get_document_includes_raw(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    row = _doc_row()
    repo.rows = (row,)
    r = client.get(f"/projects/p/documents/{row.id}")
    assert r.status_code == 200
    assert r.json()["data"]["raw"] == "full text"
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_null_status_is_omitted_never_emitted_as_null(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY (Codex round 1): the frozen Chunk.status/Document.status are
    # OPTIONAL NON-NULLABLE strings while the columns are nullable (the
    # cleaning path writes chunks with no status at all) — "status": null
    # would make an otherwise-successful inspection response schema-invalid;
    # the only legal encoding of a NULL column is key absence.
    _bindable(monkeypatch)
    repo.rows = (_chunk_row(status=None),)
    got = client.get(f"/projects/p/chunks/{uuid.uuid4()}").json()["data"]
    assert "status" not in got

    repo.rows = (_doc_row(status=None),)
    got = client.get(f"/projects/p/documents/{uuid.uuid4()}").json()["data"]
    assert "status" not in got

    repo.rows = (_chunk_row(status="embedded"),)
    got = client.get(f"/projects/p/chunks/{uuid.uuid4()}").json()["data"]
    assert got["status"] == "embedded"  # a real status still rides along


def test_missing_resource_is_a_true_404_with_the_coarse_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY (GAP, registry_errors precedent): the frozen enum has no inspect
    # not-found code, and mislabeling a missing document as PROJECT/BUILD/
    # JOB_NOT_FOUND would mislead a code-dispatching client — so the TRUE 404
    # status is preserved and the code is BA0's documented coarse 4xx mapping.
    _bindable(monkeypatch)
    repo.rows = ()
    did = uuid.uuid4()
    r = client.get(f"/projects/p/documents/{did}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"  # coarse, per BA0
    assert str(did) in r.json()["error"]["message"]

    r = client.get(f"/projects/p/chunks/{did}")
    assert r.status_code == 404


def test_binding_order_project_404_before_no_active_build_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: a missing project must never surface as NO_ACTIVE_BUILD — the 409
    # asserts the project EXISTS and merely lacks an active build.
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "get_project", missing)
    r = client.get("/projects/ghost/documents")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    async def present(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    async def no_active(conn: Any, project: str) -> Any:
        raise NoActiveBuildError(project)

    _stub(monkeypatch, "get_project", present)
    _stub(monkeypatch, "_resolve_active_binding", no_active)
    r = client.get("/projects/p/chunks")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"
    assert r.json()["error"]["details"]["project"] == "p"


def test_chunks_cursor_and_compound_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    doc = uuid.uuid4()
    rows = [_chunk_row(document_id=doc, ordinal=i) for i in range(3)]
    repo.pages = rows

    r = client.get("/projects/p/chunks", params={"limit": 2})
    body = r.json()
    token = body["meta"]["next_cursor"]
    assert decode_chunk_cursor(token) == (doc, 1)  # (document_id, ordinal) of the last in-page row
    assert body["data"][0]["ordinal"] == 0 and body["data"][1]["ordinal"] == 1


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/projects/p/documents", {"sort": "id:asc"}),  # only id:desc restates the default
        ("/projects/p/documents", {"filter[status]": "x"}),
        ("/projects/p/chunks", {"sort": "ordinal:desc"}),  # compound default: NO sort accepted
        ("/projects/p/chunks", {"filter[document_id]": "x"}),
    ],
)
def test_unsupported_sort_filter_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    repo: type[_FakeRepo],
    path: str,
    params: dict[str, str],
) -> None:
    # WHY: silently ignoring an explicit sort/filter would mislead the client
    # into trusting an order/subset it did not get (BA1b rule, extended to the
    # compound-order lists where no explicit sort can restate the default).
    _bindable(monkeypatch)
    r = client.get(path, params=params)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_documents_default_sort_may_be_restated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    assert client.get("/projects/p/documents", params={"sort": "id:desc"}).status_code == 200


def test_malformed_cursor_is_a_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    r = client.get("/projects/p/documents", params={"cursor": "not-base64!!"})
    assert r.status_code == 400
    assert "cursor" in r.json()["error"]["message"]


def _entity_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "type": "Person",
        "canonical_name": "Alice",
        "entity_key": "fpv1:person|alice",
        "attributes": None,
        "embedding_point_id": uuid.uuid4(),  # internal — must never be emitted
        "status": "active",
        "review_status": "unreviewed",
        "created_by": None,
        "created_at": _TS,
        "updated_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _relation_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "src_entity_id": uuid.uuid4(),
        "dst_entity_id": uuid.uuid4(),
        "type": "WORKS_AT",
        "attributes": None,
        "relation_signature": None,  # legitimate pre-resolve state
        "status": "active",
        "review_status": "unreviewed",
        "created_by": "llm",
        "confidence": 0.9,
        "created_at": _TS,
        "updated_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _evidence_row(relation_id: uuid.UUID, **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "relation_id": relation_id,
        "build_id": _BUILD,
        "evidence_type": "chunk",
        "evidence_ref": "doc-hash:3",
        "chunk_id": uuid.uuid4(),
        "start_offset": 0,
        "end_offset": 12,
        "quote": "Alice works.",
        "source_uri": "file:///d.txt",
        "confidence": 0.8,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_entity_dto_nullability_matrix(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY (the #55 emission rule at design time): created_by is an optional
    # NON-nullable enum over a nullable column → omit-when-null; attributes
    # coalesces to {}; the internal embedding_point_id must never leak into
    # the frozen shape.
    _bindable(monkeypatch)
    repo.rows = (_entity_row(),)
    got = client.get(f"/projects/p/entities/{uuid.uuid4()}").json()["data"]
    assert "created_by" not in got
    assert "embedding_point_id" not in got
    assert got["attributes"] == {}
    assert got["review_status"] == "unreviewed"
    assert got["updated_at"] is None  # contract-nullable stays present

    repo.rows = (_entity_row(created_by="rule", attributes={"a": 1}),)
    got = client.get(f"/projects/p/entities/{uuid.uuid4()}").json()["data"]
    assert got["created_by"] == "rule" and got["attributes"] == {"a": 1}


def test_relation_list_omits_evidence_and_null_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: evidence is detail-only (getRelation is "Get a relation WITH
    # EVIDENCE"; a list fetching N sub-resources per row would be silent
    # N+1) — and relation_signature is legitimately NULL pre-resolve, an
    # optional NON-nullable string → the key is omitted, never null.
    _bindable(monkeypatch)
    repo.pages = (_relation_row(),)
    r = client.get("/projects/p/relations")
    (rel,) = r.json()["data"]
    assert "evidence" not in rel
    assert "relation_signature" not in rel
    assert rel["created_by"] == "llm"  # non-null rides along
    assert rel["confidence"] == pytest.approx(0.9)


def test_relation_detail_carries_the_full_evidence_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    relation = _relation_row(relation_signature="fpv1:a|works_at|b")

    class _Repo(_FakeRepo):
        async def fetch_all(self, table: Any, *where: Any) -> Sequence[Any]:
            if table is not None and getattr(table, "name", "") == "relation_evidence":
                return (_evidence_row(relation.id),)
            return (relation,)

    from api.routers import inspect as inspect_module

    monkeypatch.setattr(inspect_module, "BuildScopedRepo", _Repo)
    got = client.get(f"/projects/p/relations/{relation.id}").json()["data"]
    assert got["relation_signature"] == "fpv1:a|works_at|b"
    (ev,) = got["evidence"]
    # the full frozen RelationEvidence shape — every optional field is
    # contract-nullable, so all keys present; parent context never leaks
    assert set(ev) == {
        "id",
        "evidence_type",
        "evidence_ref",
        "chunk_id",
        "start_offset",
        "end_offset",
        "quote",
        "source_uri",
        "confidence",
    }
    assert ev["quote"] == "Alice works."


def test_entities_and_relations_paginate_like_documents(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    rows = [_entity_row() for _ in range(3)]
    repo.pages = rows
    r = client.get("/projects/p/entities", params={"limit": 2})
    assert decode_id_cursor(r.json()["meta"]["next_cursor"]) == (rows[1].id,)

    rel_rows = [_relation_row() for _ in range(3)]
    repo.pages = rel_rows
    r = client.get("/projects/p/relations", params={"limit": 2})
    assert decode_id_cursor(r.json()["meta"]["next_cursor"]) == (rel_rows[1].id,)
    # and both honor the sort/filter rejection
    assert client.get("/projects/p/entities", params={"sort": "name:asc"}).status_code == 400
    assert client.get("/projects/p/relations", params={"filter[type]": "x"}).status_code == 400


def test_cursor_types_are_distinct_per_resource() -> None:
    # a documents cursor replayed on chunks must fail arity/type, not page
    # silently from the wrong keyset
    doc_token = encode_cursor((uuid.uuid4(),))
    from api.errors import ApiError

    with pytest.raises(ApiError):
        decode_chunk_cursor(doc_token)


# -- GET /projects/{p}/graph/subgraph (BA3c) ---------------------------------

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


class _FakeSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDriver:
    def session(self) -> _FakeSession:
        return _FakeSession()


def _graphable(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    *,
    config: dict[str, Any] | None = None,
) -> None:
    """Project with a (by default valid) query_policy + stubbed binding/repos
    and an overridden driver dependency — subgraph tests stub subgraph_context
    itself, so no Neo4j and no Postgres are touched."""
    from api.deps import neo4j_driver

    project_config = {"query_policy": _QUERY_POLICY} if config is None else config

    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=project_config)

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)
    _stub(monkeypatch, "BuildScopedRepo", _FakeRepo)
    _stub(monkeypatch, "BuildScopedGraphRepo", SimpleNamespace(bound_to=lambda s, b: object()))
    cast("FastAPI", client.app).dependency_overrides[neo4j_driver] = lambda: _FakeDriver()


def test_subgraph_happy_path_stamps_binding_and_shapes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _graphable(monkeypatch, client)
    seed = uuid.uuid4()
    node = {"id": str(seed), "type": "Person", "label": "Alice"}

    async def fake_context(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(nodes=(node,), edges=())

    _stub(monkeypatch, "subgraph_context", fake_context)
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(seed)})
    assert r.status_code == 200
    assert r.json()["data"] == {"nodes": [node], "edges": []}
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_subgraph_policy_missing_is_a_loud_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (owner decision 2026-07-10, strict): no invented §21 defaults — an
    # unconfigured project is told exactly what to configure, never silently
    # capped by values the contract never froze.
    _graphable(monkeypatch, client, config={})
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(uuid.uuid4())})
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"query_policy": "missing"}
    assert "query_policy" in r.json()["error"]["message"]


def test_subgraph_invalid_policy_is_a_loud_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = dict(_QUERY_POLICY, max_graph_hops=0)  # violates the frozen minimum
    _graphable(monkeypatch, client, config={"query_policy": bad})
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(uuid.uuid4())})
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"query_policy": "invalid"}


def test_subgraph_hops_beyond_ceiling_rejected_not_clamped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _graphable(monkeypatch, client)
    r = client.get(
        "/projects/p/graph/subgraph",
        params={"entity_id": str(uuid.uuid4()), "hops": 4},
    )
    assert r.status_code == 400
    assert r.json()["error"]["details"]["max_graph_hops"] == 3


def test_subgraph_unknown_seed_404_and_store_outage_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from neo4j.exceptions import ServiceUnavailable

    _graphable(monkeypatch, client)

    async def none_context(*args: Any, **kwargs: Any) -> Any:
        return None

    _stub(monkeypatch, "subgraph_context", none_context)
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(uuid.uuid4())})
    assert r.status_code == 404  # inactive/unknown entity = lookup miss, not empty-200

    async def outage(*args: Any, **kwargs: Any) -> Any:
        raise ServiceUnavailable("boom")

    _stub(monkeypatch, "subgraph_context", outage)
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(uuid.uuid4())})
    # WHY: the graph projection is a derived STORE — its outage is an honest
    # 503, never a silent empty subgraph and never a coarse 500
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "STORE_UNAVAILABLE"


def test_subgraph_entity_id_is_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _graphable(monkeypatch, client)
    assert client.get("/projects/p/graph/subgraph").status_code == 400  # missing required param


def test_subgraph_client_limit_narrows_never_widens_the_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (§21): the client's limit can only NARROW the policy row ceiling —
    # min(limit, policy.max_rows) — a limit above the policy cap must not
    # widen what §21 froze as the project's ceiling.
    _graphable(monkeypatch, client)
    captured: list[Any] = []

    async def capture_context(graph: Any, repo: Any, policy: Any, *args: Any, **kw: Any) -> Any:
        captured.append(policy.max_rows)
        return SimpleNamespace(nodes=(), edges=())

    _stub(monkeypatch, "subgraph_context", capture_context)
    seed = str(uuid.uuid4())
    client.get("/projects/p/graph/subgraph", params={"entity_id": seed, "limit": 5})
    client.get("/projects/p/graph/subgraph", params={"entity_id": seed, "limit": 500})
    # policy max_rows is 100 (_QUERY_POLICY.text_to_cypher): 5 narrows, 500 clamps
    assert captured == [5, 100]
