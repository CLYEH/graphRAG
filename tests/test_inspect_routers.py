"""Why: BA3a's routers own the HTTP behaviors above the DR-006 repo — the
project-404-before-binding-409 ordering, the §15 envelope with meta.build_id
stamped from the ACTIVE binding (the API's first active-build consumer), the
opaque keyset cursors, the contract-licensed `raw`-on-detail-only key, and the
not-found GAP mapping (true 404 status + coarse frozen code — the enum has no
inspect not-found code yet). These hold without Postgres: the binding and repo
are stubbed; the live SQL/scope behavior is the integration suite's job.
"""

from __future__ import annotations

import base64
import json
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
from api.pagination import (
    decode_chunk_cursor,
    decode_id_cursor,
    decode_scoped_id_cursor,
    encode_cursor,
    encode_sorted_cursor,
)
from api.routers.inspect import _scope_fingerprint
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
        # config rides along like the real row (the documents endpoint reads
        # metadata_schema's filterable attributes from it)
        return SimpleNamespace(name=name, config={})

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)


class _FakeRepo:
    """Captures fetch_page/fetch_all/fetch_count args and serves scripted rows."""

    pages: Sequence[Any] = ()
    rows: Sequence[Any] = ()
    total: int = 0
    calls: list[dict[str, Any]] = []
    count_calls: list[dict[str, Any]] = []

    @classmethod
    def bound_to(cls, conn: Any, binding: Any) -> _FakeRepo:
        return cls()

    async def fetch_page(self, table: Any, *where: Any, order_by: Any, limit: int) -> Sequence[Any]:
        type(self).calls.append({"where": where, "order_by": order_by, "limit": limit})
        return type(self).pages

    async def fetch_all(self, table: Any, *where: Any) -> Sequence[Any]:
        return type(self).rows

    async def fetch_count(self, table: Any, *where: Any) -> int:
        type(self).count_calls.append({"where": where})
        return type(self).total


@pytest.fixture()
def repo(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRepo]:
    _FakeRepo.pages, _FakeRepo.rows, _FakeRepo.calls = (), (), []
    _FakeRepo.total, _FakeRepo.count_calls = 0, []
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
    tag = f"id:desc|{_scope_fingerprint(None, {})}"  # R8: default mints carry scope
    assert decode_scoped_id_cursor(token, tag) == (rows[1].id,)  # last IN-PAGE row, not the probe
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
        # SS1a: filter[status] became documents' implemented facet — the
        # reject pin moved to a field OUTSIDE the allowlist
        ("/projects/p/documents", {"filter[mime]": "x"}),
        ("/projects/p/chunks", {"sort": "ordinal:desc"}),  # compound default: NO sort accepted
        ("/projects/p/chunks", {"filter[document_id]": "x"}),
        # SS1a: closed-vocabulary facets refuse out-of-vocabulary values, open
        # ones refuse blanks, and every facet refuses ambiguity (repeats)
        ("/projects/p/entities", {"filter[status]": "bogus"}),
        ("/projects/p/entities", {"filter[review_status]": "bogus"}),
        ("/projects/p/entities", {"filter[type]": "   "}),
        ("/projects/p/relations", {"filter[status]": "bogus"}),
        # GOV2-facet: confidence/evidence are CLOSED single-member vocabularies
        # ("low" / "missing") — a numeric threshold or "present" is refused, so
        # the predicate can only ever be the §19 gauge's own (no client-side
        # threshold drift)
        ("/projects/p/relations", {"filter[confidence]": "1"}),
        ("/projects/p/relations", {"filter[evidence]": "present"}),
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
    tag = f"id:desc|{_scope_fingerprint(None, {})}"  # R8: default mints carry scope
    assert decode_scoped_id_cursor(r.json()["meta"]["next_cursor"], tag) == (rows[1].id,)

    rel_rows = [_relation_row() for _ in range(3)]
    repo.pages = rel_rows
    r = client.get("/projects/p/relations", params={"limit": 2})
    assert decode_id_cursor(r.json()["meta"]["next_cursor"]) == (rel_rows[1].id,)
    # and both honor the sort/filter rejection — filter[type] became a legal
    # SS1a facet, so the reject pin uses a non-allowlisted field instead
    assert client.get("/projects/p/entities", params={"sort": "name:asc"}).status_code == 400
    assert client.get("/projects/p/relations", params={"filter[weight]": "x"}).status_code == 400


def test_entities_list_emits_exact_total(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # SS1b: a list frame carries the matching-row total so the Console can show
    # "N results" without walking every page. It is EXACT (total_estimated
    # false — the estimate path is the deferred large-table follow-up).
    _bindable(monkeypatch)
    repo.pages = (_entity_row(), _entity_row())
    repo.total = 42
    body = client.get("/projects/p/entities").json()
    assert body["meta"]["total"] == 42
    assert body["meta"]["total_estimated"] is False


def test_entities_search_filters_page_and_count_together(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # SS1b: `q` searches canonical_name and MUST narrow both the page and the
    # count — a total that ignored `q` would claim more results than the search
    # returns. Pin that the same search predicate reached fetch_count and
    # fetch_page (one predicate each beyond the keyset).
    _bindable(monkeypatch)
    repo.pages = (_entity_row(),)
    repo.total = 1
    r = client.get("/projects/p/entities", params={"q": "區域"})
    assert r.status_code == 200
    # the count saw the search predicate (no cursor → search is its only filter)
    assert len(repo.count_calls[0]["where"]) == 1
    # the page saw the SAME search predicate (no cursor here either)
    assert len(repo.calls[0]["where"]) == 1


def test_documents_search_is_supported_but_relations_and_chunks_reject_q(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # SS1b/contract: `q` is declared only on entities+documents. An endpoint
    # that cannot honor `q` must reject it LOUD (400), never silently ignore it
    # (the GAPS-O4 false-affordance discipline) — otherwise a client believes a
    # search took effect over the whole list.
    _bindable(monkeypatch)
    repo.pages = (_doc_row(),)
    repo.total = 1
    assert client.get("/projects/p/documents", params={"q": "corpus"}).status_code == 200
    for path in ("/projects/p/relations", "/projects/p/chunks"):
        r = client.get(path, params={"q": "x"})
        assert r.status_code == 400, path
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_blank_and_overlong_q_are_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # the contract types `q` minLength 1 / maxLength 256; FastAPI enforces both,
    # reshaped to the frozen 400 envelope — an empty search is a client bug, not
    # a match-everything.
    _bindable(monkeypatch)
    assert client.get("/projects/p/entities", params={"q": ""}).status_code == 400
    assert client.get("/projects/p/entities", params={"q": "x" * 257}).status_code == 400


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


def test_subgraph_no_active_build_beats_policy_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (Codex #57 R1, surface consistency): this endpoint serves the
    # ACTIVE build like every other inspect op — a project without one is a
    # 409 NO_ACTIVE_BUILD even when its policy is ALSO missing; a 400 would
    # send the client off to fix policy when there is no graph to inspect.
    from core.stores.repo import NoActiveBuildError as _NoActive

    _graphable(monkeypatch, client, config={})  # policy missing too

    async def no_active(conn: Any, project: str) -> Any:
        raise _NoActive(project)

    _stub(monkeypatch, "_resolve_active_binding", no_active)
    r = client.get("/projects/p/graph/subgraph", params={"entity_id": str(uuid.uuid4())})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"


def test_ss1a_facets_are_accepted_on_their_endpoints(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    """SS1a: the implemented facets pass the guard — a 200 here is the
    over-block half of the fail-loud pin above (class 9 對偶: deny rules need
    accept pins, or tightening silently swallows the feature)."""
    _bindable(monkeypatch)
    for path, params in (
        ("/projects/p/documents", {"filter[status]": "ingested"}),
        ("/projects/p/entities", {"filter[type]": "EXHIBIT"}),
        ("/projects/p/entities", {"filter[status]": "active"}),
        ("/projects/p/entities", {"filter[review_status]": "unreviewed"}),
        ("/projects/p/relations", {"filter[type]": "works_with"}),
        ("/projects/p/relations", {"filter[status]": "needs_review"}),
        # GOV2-facet quality facets (accept pin for the closed vocabularies)
        ("/projects/p/relations", {"filter[confidence]": "low"}),
        ("/projects/p/relations", {"filter[evidence]": "missing"}),
    ):
        r = client.get(path, params=params)
        assert r.status_code == 200, (path, params, r.text)


def test_facet_vocabularies_match_the_ddl() -> None:
    """The router's closed vocabularies must equal the DDL CHECK constraints
    VERBATIM — a drifted tuple would refuse a value the column holds (silent
    under-serving) or accept one it cannot (a facet that can never match).
    Parity is pinned mechanically by parsing the same DDL the database
    enforces (the two-gate shared-corpus rule, class 16)."""
    import re as _re

    from api.routers.inspect import LIFECYCLE_STATUS, REVIEW_STATUS
    from core.stores import tables as _tables

    def _check_values(table: object, name: str) -> tuple[str, ...]:
        for constraint in table.constraints:  # type: ignore[attr-defined]
            if getattr(constraint, "name", None) == name:
                return tuple(_re.findall(r"'([^']+)'", str(constraint.sqltext)))
        raise AssertionError(f"constraint {name} not found")

    assert set(LIFECYCLE_STATUS) == set(_check_values(_tables.entities, "entities_status_valid"))
    assert set(LIFECYCLE_STATUS) == set(_check_values(_tables.relations, "relations_status_valid"))
    assert set(REVIEW_STATUS) == set(
        _check_values(_tables.entities, "entities_review_status_valid")
    )
    assert set(REVIEW_STATUS) == set(
        _check_values(_tables.relations, "relations_review_status_valid")
    )


# ---- SS1b sort expansion + metadata filterable facets ---------------------------


def _filterable_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding stub whose project config declares filterable metadata attrs."""

    config = {
        "metadata_schema": {
            "attributes": {
                "topic": {"type": "string", "filterable": True},
                "rating": {"type": "number", "filterable": True},
                "public": {"type": "boolean", "filterable": True},
                "note": {"type": "string"},  # NOT filterable
                "status": {"type": "string", "filterable": True},  # reserved name
            }
        }
    }

    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=config)

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)


@pytest.mark.parametrize(
    ("path", "sort"),
    [
        ("/projects/p/entities", "canonical_name:asc"),
        ("/projects/p/entities", "canonical_name:desc"),
        ("/projects/p/entities", "created_at:asc"),
        ("/projects/p/entities", "created_at:desc"),
        ("/projects/p/documents", "ingested_at:asc"),
        ("/projects/p/documents", "ingested_at:desc"),
    ],
)
def test_ss1b_sorts_are_accepted_on_their_endpoints(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    repo: type[_FakeRepo],
    path: str,
    sort: str,
) -> None:
    # WHY: the owner-approved minimal sort set - accepted spellings must map
    # to a real ORDER BY (the gate and the implementation share the spelling,
    # so an accepted-but-unimplemented sort cannot exist)
    _bindable(monkeypatch)
    r = client.get(path, params={"sort": sort})
    assert r.status_code == 200, r.text
    field = sort.split(":")[0]
    order_strs = [str(o) for o in repo.calls[-1]["order_by"]]
    assert any(field in o for o in order_strs), order_strs
    direction = "ASC" if sort.endswith(":asc") else "DESC"
    assert all(direction in o for o in order_strs), order_strs  # tie-break same way


def test_sorted_page_mints_a_tagged_cursor_bound_to_its_sort(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: a keyset token only means something relative to its ORDER BY - the
    # sort rides inside the cursor, and replaying it under another sort is a
    # loud 400, never a silent re-anchor (or a name parsed as a timestamp)
    _bindable(monkeypatch)
    rows = [_entity_row() for _ in range(3)]
    repo.pages = rows
    r = client.get("/projects/p/entities", params={"limit": 2, "sort": "canonical_name:asc"})
    token = r.json()["meta"]["next_cursor"]
    assert token is not None

    ok = client.get("/projects/p/entities", params={"sort": "canonical_name:asc", "cursor": token})
    assert ok.status_code == 200

    crossed = client.get("/projects/p/entities", params={"sort": "created_at:asc", "cursor": token})
    assert crossed.status_code == 400
    assert "issued under" in crossed.json()["error"]["message"]


def test_legacy_default_cursor_still_pages_and_rejects_sorted_reuse(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # the pre-SS1b untagged (id,) cursor keeps working for the default order,
    # and crossing it into a sorted request fails the arity check
    _bindable(monkeypatch)
    legacy = encode_cursor((uuid.uuid4(),))
    assert client.get("/projects/p/entities", params={"cursor": legacy}).status_code == 200
    crossed = client.get(
        "/projects/p/entities", params={"sort": "canonical_name:asc", "cursor": legacy}
    )
    assert crossed.status_code == 400


def test_unknown_sort_fields_stay_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _bindable(monkeypatch)
    r = client.get("/projects/p/entities", params={"sort": "confidence:asc"})
    assert r.status_code == 400
    assert "supported sorts" in r.json()["error"]["message"]


def test_filterable_metadata_attr_filters_by_containment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: rule 8 search half - ONLY schema-declared filterable attrs become
    # facets, matched by JSONB containment down the envelope path (the shape
    # the GIN index serves)
    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[topic]": "sea"})
    assert r.status_code == 200, r.text
    where_strs = [str(w) for w in repo.calls[-1]["where"]]
    assert any("@>" in w for w in where_strs), where_strs


def test_non_filterable_attr_is_rejected_loud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # `note` exists in the schema but is NOT filterable - accepting it would
    # promise an index-backed facet the schema never opted into
    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[note]": "x"})
    assert r.status_code == 400
    assert "supported filters" in r.json()["error"]["message"]


def test_metadata_number_and_boolean_values_are_typed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    _filterable_project(monkeypatch)
    # declared number: a non-number is a caller error, not a match-nothing
    assert client.get("/projects/p/documents", params={"filter[rating]": "abc"}).status_code == 400
    assert client.get("/projects/p/documents", params={"filter[rating]": "5"}).status_code == 200
    # declared boolean: STRICT true/false (class 1 - "True" is a typo)
    assert client.get("/projects/p/documents", params={"filter[public]": "True"}).status_code == 400
    assert client.get("/projects/p/documents", params={"filter[public]": "true"}).status_code == 200


def test_metadata_attr_named_status_is_reserved_for_the_lifecycle_facet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # WHY: filter[status] predates the metadata facets - letting a schema attr
    # shadow it would silently flip an existing spelling meaning; the
    # lifecycle facet wins and the attr is unreachable under this name
    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[status]": "ingested"})
    assert r.status_code == 200
    where_strs = [str(w) for w in repo.calls[-1]["where"]]
    assert not any("@>" in w for w in where_strs), where_strs


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity", "1e999"])
def test_non_finite_number_filters_are_rejected_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo], bad: str
) -> None:
    # Codex #120 R1: NaN/Infinity/overflow parse as floats but JSONB cannot
    # carry them - unchecked they 500 at bind time; a client-controlled
    # filter value must be a typed 400 (the uploads _finite_float twin)
    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[rating]": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_malformed_metadata_schema_is_a_400_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R1: a malformed metadata_schema in the freely writable config
    # is operator-fixable - the documents list must say so (typed 400, the
    # uploads/query discipline), never turn every request into an opaque 500
    config = {"metadata_schema": {"attributes": {"t": {"filterable": True}}}}  # missing type

    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=config)

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)

    r = client.get("/projects/p/documents")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "type is required" in r.json()["error"]["message"]


def test_nul_in_metadata_filter_is_rejected_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R2: PostgreSQL JSONB cannot represent U+0000 - a %00 in a
    # client filter would 500 at bind time; rejected for EVERY declared type
    # before the containment predicate forms (the uploads _contains_nul twin)
    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[topic]": "\x00sea"})
    assert r.status_code == 400
    assert "NUL" in r.json()["error"]["message"]


def test_tampered_naive_datetime_cursor_is_a_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R2: a correctly TAGGED cursor whose timestamp value was
    # tampered to a timezone-less ISO string parses fine but would raise in
    # the asyncpg timestamptz encoder (500) - it must be the documented
    # malformed-cursor 400 instead
    _bindable(monkeypatch)
    tag = f"created_at:asc|{_scope_fingerprint(None, {})}"
    forged = encode_sorted_cursor(tag, ("2026-01-01T00:00:00", uuid.uuid4()))
    r = client.get("/projects/p/entities", params={"sort": "created_at:asc", "cursor": forged})
    assert r.status_code == 400
    assert "malformed cursor" in r.json()["error"]["message"]


@pytest.mark.parametrize(
    "bad", [123, True, {"a": 1}, ["x"]], ids=["number", "bool", "object", "array"]
)
def test_non_string_uuid_cursor_part_is_a_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo], bad: Any
) -> None:
    # Codex #120 R4: a correctly TAGGED cursor whose uuid tie-break slot was
    # tampered to a JSON number/bool/object/array reaches uuid.UUID(), which
    # dies with AttributeError - outside the ValueError/TypeError translation,
    # so it was a 500; must be the documented malformed-cursor 400
    _bindable(monkeypatch)
    # hand-rolled: encode_cursor str()-ifies every value, so the raw JSON
    # shapes only exist in a token a client BUILT, never one the server minted
    tag = f"created_at:asc|{_scope_fingerprint(None, {})}"
    raw = json.dumps([tag, "2026-01-01T00:00:00+00:00", bad]).encode()
    forged = base64.urlsafe_b64encode(raw).decode()
    r = client.get("/projects/p/entities", params={"sort": "created_at:asc", "cursor": forged})
    assert r.status_code == 400
    assert "malformed cursor" in r.json()["error"]["message"]


def test_no_active_build_outranks_malformed_metadata_schema(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R5: an existing project with BOTH no active build and a
    # malformed metadata_schema must surface the documented bootstrap 409 -
    # a config 400 first would point bootstrap users at the wrong problem
    config = {"metadata_schema": {"attributes": {"t": {"filterable": True}}}}  # missing type

    async def bad_config_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=config)

    async def no_active(conn: Any, project: str) -> Any:
        raise NoActiveBuildError(project)

    _stub(monkeypatch, "get_project", bad_config_project)
    _stub(monkeypatch, "_resolve_active_binding", no_active)

    r = client.get("/projects/p/documents")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"


@pytest.mark.parametrize("bad", ["a\u0000b", 123, {"a": 1}], ids=["nul", "number", "object"])
def test_invalid_text_cursor_part_is_a_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo], bad: Any
) -> None:
    # Codex #120 R7 (the R2/R4 tampered-cursor family, text slot): PostgreSQL
    # text cannot carry U+0000, so a tagged cursor with a NUL in the
    # canonical_name slot reaches the tuple bind and dies server-side; and a
    # non-str JSON shape would be silently repr-coerced by str(). Both must
    # be the documented malformed-cursor 400.
    _bindable(monkeypatch)
    tag = f"canonical_name:asc|{_scope_fingerprint(None, {})}"
    raw = json.dumps([tag, bad, str(uuid.uuid4())]).encode()
    forged = base64.urlsafe_b64encode(raw).decode()
    r = client.get("/projects/p/entities", params={"sort": "canonical_name:asc", "cursor": forged})
    assert r.status_code == 400
    assert "malformed cursor" in r.json()["error"]["message"]


def test_cursor_rejects_replay_under_a_different_search_or_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R8: a keyset anchor positions within ONE result set - the
    # same sort with a different q (or filter) is a DIFFERENT result set, so
    # replaying the token there would silently skip or duplicate rows; the
    # scope fingerprint rides in the tag and mismatches 400, exactly like
    # cross-sort reuse
    _bindable(monkeypatch)
    rows = [_entity_row() for _ in range(3)]
    repo.pages = rows
    r = client.get(
        "/projects/p/entities",
        params={"limit": 2, "sort": "canonical_name:asc", "q": "alpha"},
    )
    token = r.json()["meta"]["next_cursor"]
    assert token is not None

    same = client.get(
        "/projects/p/entities",
        params={"sort": "canonical_name:asc", "q": "alpha", "cursor": token},
    )
    assert same.status_code == 200  # over-block guard: the SAME scope pages on

    crossed_q = client.get(
        "/projects/p/entities",
        params={"sort": "canonical_name:asc", "q": "beta", "cursor": token},
    )
    assert crossed_q.status_code == 400
    assert "issued under" in crossed_q.json()["error"]["message"]

    dropped_q = client.get(
        "/projects/p/entities", params={"sort": "canonical_name:asc", "cursor": token}
    )
    assert dropped_q.status_code == 400  # dropping the search also changes the set


def test_default_order_cursor_rejects_filter_swap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R8, default (id desc) order: newly minted cursors carry the
    # scope tag too - a filter[status] swap mid-pagination is a 400, while
    # the identical filter keeps paging
    _bindable(monkeypatch)
    rows = [_doc_row() for _ in range(3)]
    repo.pages = rows
    r = client.get("/projects/p/documents", params={"limit": 2, "filter[status]": "ingested"})
    token = r.json()["meta"]["next_cursor"]
    assert token is not None

    same = client.get(
        "/projects/p/documents", params={"filter[status]": "ingested", "cursor": token}
    )
    assert same.status_code == 200

    swapped = client.get(
        "/projects/p/documents", params={"filter[status]": "failed", "cursor": token}
    )
    assert swapped.status_code == 400
    assert "issued under" in swapped.json()["error"]["message"]


def test_numeric_filter_binds_without_float_precision_loss(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: type[_FakeRepo]
) -> None:
    # Codex #120 R8: 9007199254740993.0 through binary float rounds to ...992
    # and JSONB compares NUMERICALLY - the probe must carry the exact value.
    # Integral spellings bind as exact ints; fractional ones as float only
    # when the float means the same number; the rest are a typed 400.
    from api.routers.inspect import _typed_metadata_value

    exact = _typed_metadata_value("9007199254740993.0", "number", "rating")
    assert exact == 9007199254740993 and isinstance(exact, int)
    assert _typed_metadata_value("3.14", "number", "rating") == 3.14
    assert _typed_metadata_value("-2.5e2", "number", "rating") == -250

    _filterable_project(monkeypatch)
    r = client.get("/projects/p/documents", params={"filter[rating]": "0.10000000000000000555"})
    assert r.status_code == 400
    assert "float precision" in r.json()["error"]["message"]
