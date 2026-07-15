"""Why: the eval endpoint's whole job is to run the project's golden set against
a NAMED build as an async job and land the score where the activation gate reads
it — "zero new coupling" (DR-010). So the behaviors that matter are the job
contract: a 202 + job envelope on acceptance, the single-active-job 409
(overlapping work must not double-run an eval), the project 404, and — the
correlation that makes the whole feature work — that the build named in the PATH
is exactly what gets enqueued for the worker to evaluate.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import arq_redis_provider, db_conn
from core.registry import JobConflictError, ProjectNotFoundError

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()
_URL = f"/projects/demo/builds/{_BUILD}/eval"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    def _fake_queue() -> Callable[[], Awaitable[object]]:
        async def _get() -> object:
            return object()

        return _get

    app.dependency_overrides[db_conn] = _conn
    app.dependency_overrides[arq_redis_provider] = _fake_queue
    with TestClient(app) as c:
        yield c


def _created_job(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub create_job_exclusive to return a queued job, capturing its kwargs."""
    captured: dict[str, Any] = {}

    async def fake_create(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        captured["kind"] = kind
        captured["build_id"] = build_id
        return SimpleNamespace(id=uuid.uuid4(), status="queued")

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", fake_create)
    return captured


def _capture_enqueue(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_enqueue(redis: Any, project: str, job_id: Any, build_id: Any) -> bool:
        captured["project"] = project
        captured["job_id"] = job_id
        captured["build_id"] = build_id
        return True

    monkeypatch.setattr("api.routers.builds.enqueue_eval", fake_enqueue)
    return captured


def test_eval_returns_202_and_job_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _created_job(monkeypatch)
    _capture_enqueue(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "queued" and "job_id" in data


def test_eval_records_kind_and_target_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _created_job(monkeypatch)
    enqueued = _capture_enqueue(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    # the job is an 'eval' job bound to the build named in the path...
    assert created["kind"] == "eval"
    assert created["build_id"] == _BUILD
    # ...and THAT build is what the worker is handed (the correlation that makes
    # the score land on the right build's eval column)
    assert enqueued["build_id"] == _BUILD


def test_eval_overlapping_job_is_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def conflict(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise JobConflictError(project, uuid.uuid4())

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", conflict)
    resp = client.post(_URL)
    assert resp.status_code == 409


def test_eval_unknown_project_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise ProjectNotFoundError(project)

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", missing)
    resp = client.post(_URL)
    assert resp.status_code == 404
