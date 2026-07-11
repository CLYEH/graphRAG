"""Why: BA8's routers are the frozen Build facade over the core lifecycle —
these tests pin the HTTP orchestration: the field-for-field Build DTO
(checklist item 5's named-list diff), the 404 precedence (project → build →
preflight 409), the seam wiring (allow_archived / apply_eval_gate per
endpoint — the TARGETED rollback and its archived-only history exemption),
the RAISE-on-failure rule (a stored 409 would poison the §27 key), and
class 13 (broken store config must not poison the 404s). The promotion
machinery itself is core's (test_builds_lifecycle / the integration e2e).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from api.app import create_app
from api.deps import db_conn
from core.builds.lifecycle import BuildInfo, PreflightReport

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()
_NOW = datetime(2026, 7, 11, tzinfo=UTC)

_FROZEN_BUILD_FIELDS = {
    "id",
    "project",
    "status",
    "config_hash",
    "source_hash",
    "started_at",
    "finished_at",
    "activated_at",
    "metrics",
    "eval",
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.builds.{name}", fn)


def _project_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    _stub(monkeypatch, "get_project", fake_get_project)


def _build(status: str = "ready", **over: Any) -> BuildInfo:
    base: dict[str, Any] = {
        "id": _BUILD,
        "status": status,
        "started_at": _NOW,
        "finished_at": None,
        "activated_at": None,
        "project": "p",
        "config_hash": None,
        "source_hash": "s" * 8,
        "metrics": None,
        "eval": {"score": 0.8},
    }
    base.update(over)
    return BuildInfo(**base)


def _known_build(monkeypatch: pytest.MonkeyPatch, build: BuildInfo) -> None:
    async def fake_get_build(conn: Any, project: str, build_id: Any) -> BuildInfo:
        return build

    _stub(monkeypatch, "get_build_info", fake_get_build)


def test_build_dto_is_the_frozen_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # WHY (item 5's named-list diff): every frozen Build field present —
    # nullable fields EMIT null (contract-nullable, never omit-when-null).
    _project_exists(monkeypatch)
    _known_build(monkeypatch, _build())
    r = client.get(f"/projects/p/builds/{_BUILD}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == _FROZEN_BUILD_FIELDS
    assert data["config_hash"] is None and data["metrics"] is None  # null, not absent
    assert data["status"] == "ready" and data["eval"] == {"score": 0.8}
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_list_paginates_and_rejects_unsupported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_exists(monkeypatch)
    next_id = uuid.uuid4()

    async def fake_page(conn: Any, project: str, *, limit: int, after_id: Any) -> Any:
        return [_build()], next_id

    _stub(monkeypatch, "list_builds_page", fake_page)
    r = client.get("/projects/p/builds")
    assert r.status_code == 200
    assert [b["id"] for b in r.json()["data"]] == [str(_BUILD)]
    assert r.json()["meta"]["next_cursor"]  # opaque, non-null mid-stream
    # the BA3 list convention: filter[...]/non-default sort reject loudly
    assert client.get("/projects/p/builds", params={"filter[status]": "ready"}).status_code == 400
    assert client.get("/projects/p/builds", params={"sort": "started_at:desc"}).status_code == 400
    assert client.get("/projects/p/builds", params={"sort": "id:desc"}).status_code == 200


def test_404_precedence_and_class13(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # unknown project → PROJECT_NOT_FOUND on every surface
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "get_project", missing)
    for method, path in (
        ("GET", "/projects/ghost/builds"),
        ("GET", f"/projects/ghost/builds/{_BUILD}"),
        ("POST", f"/projects/ghost/builds/{_BUILD}/activate"),
        ("POST", f"/projects/ghost/builds/{_BUILD}/rollback"),
    ):
        r = client.request(method, path)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "PROJECT_NOT_FOUND")

    # known project, unknown build → BUILD_NOT_FOUND — and the class-13 pin:
    # store acquisition raising must not be reachable before the 404
    _project_exists(monkeypatch)

    async def no_build(conn: Any, project: str, build_id: Any) -> None:
        return None

    def boom(request: Any) -> Any:
        raise ValueError("invalid store config")

    _stub(monkeypatch, "get_build_info", no_build)
    _stub(monkeypatch, "qdrant_client", boom)
    _stub(monkeypatch, "neo4j_driver", boom)
    for path in (f"/projects/p/builds/{_BUILD}/activate", f"/projects/p/builds/{_BUILD}/rollback"):
        r = client.post(path)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "BUILD_NOT_FOUND")


class _FakeSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _stores_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_qdrant(request: Any) -> object:
        return object()

    async def fake_driver(request: Any) -> Any:
        return SimpleNamespace(session=lambda: _FakeSession())

    _stub(monkeypatch, "qdrant_client", fake_qdrant)
    _stub(monkeypatch, "neo4j_driver", fake_driver)


def test_seam_wiring_and_gate_exemption(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: /activate gates always; /rollback exempts the §20 gate ONLY for an
    # archived (history-restore) target — a ready target through /rollback is
    # a fresh promotion (exempting it would be a §20 bypass).
    _project_exists(monkeypatch)
    _stores_ok(monkeypatch)
    captured: dict[str, Any] = {}

    def _arm(target_status: str) -> None:
        _known_build(monkeypatch, _build(status=target_status))

    async def fake_seam(
        conn: Any, qdrant: Any, session: Any, project: str, build_id: Any, **kw: Any
    ) -> PreflightReport:
        captured.update(kw)
        return PreflightReport((), ())

    _stub(monkeypatch, "activate_in_caller_txn", fake_seam)

    _arm("ready")
    assert client.post(f"/projects/p/builds/{_BUILD}/activate").status_code == 200
    assert captured == {"allow_archived": False, "apply_eval_gate": True}

    _arm("archived")
    assert client.post(f"/projects/p/builds/{_BUILD}/rollback").status_code == 200
    assert captured == {"allow_archived": True, "apply_eval_gate": False}

    _arm("ready")  # ready target through /rollback keeps the gate
    assert client.post(f"/projects/p/builds/{_BUILD}/rollback").status_code == 200
    assert captured == {"allow_archived": True, "apply_eval_gate": True}


def test_preflight_failure_and_lost_race_are_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_exists(monkeypatch)
    _stores_ok(monkeypatch)
    _known_build(monkeypatch, _build())

    async def failing_seam(*args: Any, **kw: Any) -> PreflightReport:
        return PreflightReport(("drift: pg 1 vs neo4j 0",), ("eval vacuous",))

    _stub(monkeypatch, "activate_in_caller_txn", failing_seam)
    r = client.post(f"/projects/p/builds/{_BUILD}/activate")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BUILD_NOT_READY")
    assert r.json()["error"]["details"]["failures"] == ["drift: pg 1 vs neo4j 0"]
    assert r.json()["error"]["details"]["deferred"] == ["eval vacuous"]

    async def racing_seam(*args: Any, **kw: Any) -> PreflightReport:
        raise RuntimeError("activation lost the race")

    _stub(monkeypatch, "activate_in_caller_txn", racing_seam)
    r = client.post(f"/projects/p/builds/{_BUILD}/activate")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BUILD_NOT_READY")


def test_store_outage_during_the_probe_is_a_typed_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (the BA6a-R4 preflight class on a MUTATION surface): the drift
    # probe is mandatory on every activate/rollback, so a Neo4j/Qdrant
    # outage must be the typed 503 a client can dispatch on — fail-closed
    # (nothing mutated, reservation rolls back), never the generic 500.
    # Discriminating: the unmapped shape answered 500 INTERNAL.
    _project_exists(monkeypatch)
    _stores_ok(monkeypatch)
    _known_build(monkeypatch, _build())

    async def store_down(*args: Any, **kw: Any) -> PreflightReport:
        raise ServiceUnavailable("neo4j down")

    _stub(monkeypatch, "activate_in_caller_txn", store_down)
    for path in (f"/projects/p/builds/{_BUILD}/activate", f"/projects/p/builds/{_BUILD}/rollback"):
        r = client.post(path)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "STORE_UNAVAILABLE")
