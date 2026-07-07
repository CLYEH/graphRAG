"""Why: the routers own real logic above the registry — status codes, the §15
envelope, opaque-cursor plumbing, and the domain→frozen-code error mapping —
that must hold independently of Postgres. These component tests stub the
registry layer (so no DB) and drive the handlers through the real app (real
middleware, exception handlers, param binding), pinning that orchestration; the
live SQL behavior is the integration suite's job. Idempotency-wrapped POSTs are
exercised WITHOUT a key here (the keyed path runs SQL and is integration-only).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from core.registry import (
    Project,
    ProjectExistsError,
    ProjectHasBuildsError,
    ProjectNotFoundError,
    Source,
)

pytestmark = pytest.mark.contract

_TS = datetime(2026, 7, 7, tzinfo=UTC)
_PROJECT = Project(name="p", display_name="D", description=None, config={}, created_at=_TS)
_SOURCE = Source(id=uuid.uuid4(), project="p", kind="file", uri="u", metadata={}, added_at=_TS)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()  # registry is stubbed; the connection is never used

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, module: str, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.{module}.{name}", fn)


def test_list_projects_envelope_and_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(conn: Any, *, limit: int, after: Any = None) -> Any:
        return [_PROJECT], (_TS, "p")  # a next page remains

    _stub(monkeypatch, "projects", "list_projects", fake_list)
    r = client.get("/projects")
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["name"] == "p"
    assert body["meta"]["next_cursor"]  # encoded from the (ts, name) keyset
    assert body["meta"]["build_id"] is None


def test_create_project_201_without_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create(conn: Any, **kw: Any) -> Project:
        return _PROJECT

    _stub(monkeypatch, "projects", "create_project", fake_create)
    r = client.post("/projects", json={"name": "p"})
    assert r.status_code == 201
    assert r.json()["data"]["name"] == "p"


def test_create_duplicate_maps_to_validation_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create(conn: Any, **kw: Any) -> Project:
        raise ProjectExistsError("p")

    _stub(monkeypatch, "projects", "create_project", fake_create)
    r = client.post("/projects", json={"name": "p"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"]["name"] == "p"


def test_get_project_404_and_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "projects", "get_project", missing)
    assert client.get("/projects/x").status_code == 404

    async def present(conn: Any, name: str) -> Project:
        return _PROJECT

    _stub(monkeypatch, "projects", "get_project", present)
    r = client.get("/projects/p")
    assert r.status_code == 200
    assert r.json()["data"]["display_name"] == "D"


def test_update_project_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, name: str, **kw: Any) -> None:
        return None

    _stub(monkeypatch, "projects", "update_project", missing)
    assert client.patch("/projects/x", json={"description": "d"}).status_code == 404


def test_delete_project_204_and_has_builds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", ok)
    r = client.delete("/projects/p")
    assert r.status_code == 204
    assert r.content == b""

    async def has_builds(conn: Any, name: str) -> bool:
        raise ProjectHasBuildsError("p", 2)

    _stub(monkeypatch, "projects", "delete_project", has_builds)
    r = client.delete("/projects/p")
    assert r.status_code == 400
    assert r.json()["error"]["details"]["builds"] == 2


def test_delete_project_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def gone(conn: Any, name: str) -> bool:
        return False

    _stub(monkeypatch, "projects", "delete_project", gone)
    assert client.delete("/projects/x").status_code == 404


def test_list_sources_404_when_project_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "sources", "get_project", missing)
    assert client.get("/projects/x/sources").status_code == 404


def test_add_source_201_and_missing_project(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def add_ok(conn: Any, project: str, **kw: Any) -> Source:
        return _SOURCE

    _stub(monkeypatch, "sources", "add_source", add_ok)
    r = client.post("/projects/p/sources", json={"uri": "u"})
    assert r.status_code == 201
    assert "project" not in r.json()["data"]

    async def add_missing(conn: Any, project: str, **kw: Any) -> Source:
        raise ProjectNotFoundError("x")

    _stub(monkeypatch, "sources", "add_source", add_missing)
    r = client.post("/projects/x/sources", json={"uri": "u"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"
