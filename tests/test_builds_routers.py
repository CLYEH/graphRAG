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
from api.pagination import decode_step_cursor
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


# --- RB1: step / item drill-down ----------------------------------------------


def _step_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "step_name": "extract",
        "status": "failed",
        "started_at": _NOW,
        "finished_at": _NOW,
        "input_count": 10,
        "output_count": 7,
        "skipped_count": 0,
        "failed_count": 3,
        "error": {"kind": "PartialFailure"},
    }
    base.update(over)
    return SimpleNamespace(**base)


def _item_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "item_kind": "document",
        "item_ref": "content-hash-abc",
        "status": "failed",
        "message": "unreadable",
        "error": {"exc": "OSError"},
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_list_build_steps_shape_pagination_and_build_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RB1: the step drill-down carries meta.build_id (the build named in the
    # path), pages id-desc, and emits the full BuildStep shape with nullable
    # counts as-is.
    _project_exists(monkeypatch)
    _known_build(monkeypatch, _build())
    rows = [_step_row(), _step_row()]
    # the keyset is (run started_at, step id) — newest run first (RB1/Codex #99)
    next_key = (_NOW, rows[-1].id)

    async def fake_list(
        conn: Any, project: str, build_id: Any, *, limit: int, after: Any, status: Any
    ) -> Any:
        return rows, next_key  # a next page exists

    _stub(monkeypatch, "list_build_steps", fake_list)
    r = client.get(f"/projects/p/builds/{_BUILD}/steps")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["build_id"] == str(_BUILD)
    assert decode_step_cursor(body["meta"]["next_cursor"]) == next_key
    step = body["data"][0]
    assert step["step_name"] == "extract" and step["status"] == "failed"
    assert step["failed_count"] == 3 and step["error"] == {"kind": "PartialFailure"}


def test_list_build_steps_404_when_build_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a missing build is BUILD_NOT_FOUND (via _require_build) BEFORE any step read
    _project_exists(monkeypatch)

    async def no_build(conn: Any, project: str, build_id: Any) -> Any:
        return None

    _stub(monkeypatch, "get_build_info", no_build)
    r = client.get(f"/projects/p/builds/{_BUILD}/steps")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BUILD_NOT_FOUND"


def test_list_build_steps_rejects_unsupported_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_exists(monkeypatch)
    _known_build(monkeypatch, _build())

    async def fake_list(conn: Any, *a: Any, **kw: Any) -> Any:
        return [], None

    _stub(monkeypatch, "list_build_steps", fake_list)
    # filter[status] is the implemented facet (passes); anything else fails loud
    assert (
        client.get(
            f"/projects/p/builds/{_BUILD}/steps", params={"filter[status]": "failed"}
        ).status_code
        == 200
    )
    assert (
        client.get(f"/projects/p/builds/{_BUILD}/steps", params={"filter[name]": "x"}).status_code
        == 400
    )
    assert (
        client.get(f"/projects/p/builds/{_BUILD}/steps", params={"sort": "name:asc"}).status_code
        == 400
    )
    # the order is COMPOUND (newest run first) — even `id:desc` is rejected, not
    # 200'd as a sort the endpoint doesn't honor (Codex #99 R2, GAPS-O4)
    assert (
        client.get(f"/projects/p/builds/{_BUILD}/steps", params={"sort": "id:desc"}).status_code
        == 400
    )


def test_list_step_items_404_when_step_not_in_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a step that is not part of this build is a true 404 (the build exists, the
    # step does not) — never an empty page that reads as "no items"
    _project_exists(monkeypatch)
    _known_build(monkeypatch, _build())

    async def not_in_build(conn: Any, project: str, build_id: Any, step_id: Any) -> bool:
        return False

    ran = {"n": 0}

    async def items_should_not_run(conn: Any, *a: Any, **kw: Any) -> Any:
        ran["n"] += 1
        return [], None

    _stub(monkeypatch, "step_belongs_to_build", not_in_build)
    _stub(monkeypatch, "list_step_items", items_should_not_run)
    r = client.get(f"/projects/p/builds/{_BUILD}/steps/{uuid.uuid4()}/items")
    assert r.status_code == 404
    assert ran["n"] == 0  # the existence check short-circuits before the item read


def test_list_step_items_shape_and_status_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_exists(monkeypatch)
    _known_build(monkeypatch, _build())

    async def in_build(conn: Any, project: str, build_id: Any, step_id: Any) -> bool:
        return True

    seen: dict[str, Any] = {}

    async def fake_items(
        conn: Any, project: str, build_id: Any, step_id: Any, *, limit: int, after: Any, status: Any
    ) -> Any:
        seen["status"] = status
        return [_item_row()], None

    _stub(monkeypatch, "step_belongs_to_build", in_build)
    _stub(monkeypatch, "list_step_items", fake_items)
    sid = uuid.uuid4()
    r = client.get(
        f"/projects/p/builds/{_BUILD}/steps/{sid}/items", params={"filter[status]": "failed"}
    )
    assert r.status_code == 200
    assert seen["status"] == "failed"  # the facet reached the read
    item = r.json()["data"][0]
    assert item["item_kind"] == "document" and item["item_ref"] == "content-hash-abc"
    assert item["status"] == "failed" and item["message"] == "unreadable"
