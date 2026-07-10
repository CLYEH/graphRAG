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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from api.pagination import decode_chunk_cursor, decode_document_cursor, encode_cursor
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
    assert decode_document_cursor(token) == (rows[1].id,)  # last IN-PAGE row, not the probe
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


def test_cursor_types_are_distinct_per_resource() -> None:
    # a documents cursor replayed on chunks must fail arity/type, not page
    # silently from the wrong keyset
    doc_token = encode_cursor((uuid.uuid4(),))
    from api.errors import ApiError

    with pytest.raises(ApiError):
        decode_chunk_cursor(doc_token)
