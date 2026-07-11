"""Why: BA7's routers are thin projections over ONE producer
(core.observability.health) — these tests pin the HTTP orchestration: the
frozen payload passthrough, meta.build_id naming the build the payload is
ABOUT, the 404 gate, and the deliberate ABSENCE of the query surface's 409
(an observation surface reports on bootstrap/broken states — the
"precedence belongs to the concept" lesson cuts both ways). The report
semantics themselves (§19 precedence, §20 comparability) are core's, tested
in test_observability_health.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from core.observability.health import HealthReport

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.health.{name}", fn)


def _project_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    _stub(monkeypatch, "get_project", fake_get_project)


def _report(**over: Any) -> HealthReport:
    base: dict[str, Any] = {
        "project": "p",
        "status": "Healthy",
        "active_build_id": _BUILD,
        "drift": (),
        "metrics": {"pending_review": 0},
    }
    base.update(over)
    return HealthReport(**base)


def test_health_serves_the_frozen_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the router speaks the FROZEN HealthReport (to_payload) — the
    # lower-snake enum, drift object-or-null, warnings typed — and meta names
    # the active build the counts are scoped to.
    _project_exists(monkeypatch)
    report = _report(
        status="Needs review",
        drift=("graph drift: postgres has 2 entities, neo4j 1",),
        metrics={"pending_review": 3, "documents": 5, "active_build": str(_BUILD)},
        warnings=("drift check unavailable: Neo4jError",),
    )

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        return report

    _stub(monkeypatch, "health_report", fake_report)
    r = client.get("/projects/p/health")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "needs_review"  # the frozen enum, never the display string
    assert data["pending_review"] == 3 and data["counts"]["documents"] == 5
    assert data["drift"] == {"failures": ["graph drift: postgres has 2 entities, neo4j 1"]}
    assert data["warnings"] == [
        {"code": "STORE_UNAVAILABLE", "message": "drift check unavailable: Neo4jError"}
    ]
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_metrics_reprojects_the_same_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (class 5): /metrics and /health must NEVER disagree — one producer,
    # two projections; the snapshot is the report's metrics dict verbatim.
    _project_exists(monkeypatch)
    calls: list[str] = []
    metrics = {"documents": 5, "pending_review": 2, "builds_total": 1}

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        calls.append(project)
        return _report(metrics=metrics)

    _stub(monkeypatch, "health_report", fake_report)
    r = client.get("/projects/p/metrics")
    assert r.status_code == 200
    assert r.json()["data"] == metrics
    assert r.json()["meta"]["build_id"] == str(_BUILD)
    assert calls == ["p"]  # the §19 producer served it — no second bookkeeping


def test_bootstrap_is_a_report_never_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (the precedence lesson's dual): the query surface 409s without an
    # active build because it cannot SERVE; this surface OBSERVES — a
    # bootstrap project is a legitimate healthy report with null build ids,
    # and /eval serves the all-null report (measured facts only).
    _project_exists(monkeypatch)

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        return _report(active_build_id=None)

    async def fake_eval(conn: Any, project: str) -> dict[str, Any]:
        return {"build_id": None, "passed": None, "regression": None, "metrics": {}}

    _stub(monkeypatch, "health_report", fake_report)
    _stub(monkeypatch, "latest_eval_payload", fake_eval)
    r = client.get("/projects/p/health")
    assert r.status_code == 200
    assert r.json()["data"]["active_build_id"] is None
    assert r.json()["meta"]["build_id"] is None
    r = client.get("/projects/p/eval")
    assert r.status_code == 200
    assert r.json()["data"]["build_id"] is None
    assert r.json()["meta"]["build_id"] is None


def test_eval_serves_the_latest_report(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _project_exists(monkeypatch)
    payload = {
        "build_id": str(_BUILD),
        "passed": True,
        "regression": False,
        "metrics": {"groundedness": 0.9},
    }

    async def fake_eval(conn: Any, project: str) -> dict[str, Any]:
        return payload

    _stub(monkeypatch, "latest_eval_payload", fake_eval)
    r = client.get("/projects/p/eval")
    assert r.status_code == 200
    assert r.json()["data"] == payload
    assert r.json()["meta"]["build_id"] == str(_BUILD)  # the build the report is ABOUT


def test_broken_store_config_cannot_poison_the_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (Codex #62, the #53 R3 eager-acquisition class): the projection
    # stores are PROVIDERS invoked only when the drift probe runs — a missing
    # project must 404 even when Neo4j/Qdrant construction would raise.
    # Discriminating: the old shape resolved them as route dependencies, so
    # this request answered 500 before the 404.
    async def missing(conn: Any, name: str) -> None:
        return None

    def boom(request: Any) -> Any:
        raise ValueError("invalid store config")

    _stub(monkeypatch, "get_project", missing)
    _stub(monkeypatch, "qdrant_client", boom)
    _stub(monkeypatch, "neo4j_driver", boom)
    r = client.get("/projects/ghost/health")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize("path", ["health", "metrics", "eval"])
def test_unknown_project_is_404_on_every_surface(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    async def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    _stub(monkeypatch, "get_project", missing)
    _stub(monkeypatch, "health_report", must_not_run)
    _stub(monkeypatch, "latest_eval_payload", must_not_run)
    r = client.get(f"/projects/ghost/{path}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"
