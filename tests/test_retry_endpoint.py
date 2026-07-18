"""Why: the retry endpoint (RB1-retry-core, DR-013) opens a NEW build to reprocess
a terminal ``failed`` build's failed items, so the behaviors that matter are the
job contract + the lineage/guard that make it a RETRY and not a rebuild or a
corruption of history: a 202 + job envelope, the child recording
``parent_build_id`` (never mutating the parent), the ``retry`` kind bound to the
CHILD build (what the worker resumes), the 409 ``BUILD_NOT_RETRYABLE`` for any
non-``failed`` status (a running/ready/active build must not be retried), the
single-active-job 409, the project/build 404s, and the loud rejection of an
unhonored ``reason``.

Component-level (fake conn/redis, stubbed core seams): the id-remapping clone and
the JobConflict-rolls-back-the-child atomicity are proved on live SQL in
test_builds_retry_clone_integration.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import arq_redis_provider, db_conn
from core.builds.lifecycle import BuildInfo
from core.builds.retry import CloneCounts
from core.registry import JobConflictError, ProjectNotFoundError

pytestmark = pytest.mark.contract

_PARENT = uuid.uuid4()
_URL = f"/projects/demo/builds/{_PARENT}/retry"
_NOW = datetime(2026, 7, 18, tzinfo=UTC)


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


def _build_info(status: str) -> BuildInfo:
    return BuildInfo(
        id=_PARENT,
        status=status,
        started_at=_NOW,
        finished_at=_NOW,
        activated_at=None,
        project="demo",
    )


def _stub_parent(monkeypatch: pytest.MonkeyPatch, *, status: str = "failed") -> None:
    """The parent build exists (project + build 404 gates pass) with ``status``."""

    async def a_project(conn: object, name: str, *, for_update: bool = False) -> object:
        return object()

    async def the_build(conn: object, project: str, build_id: uuid.UUID) -> BuildInfo:
        return _build_info(status)

    monkeypatch.setattr("api.routers.builds.get_project", a_project)
    monkeypatch.setattr("api.routers.builds.get_build_info", the_build)


def _stub_produce(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the child creation + clone + job + enqueue, capturing their args."""
    captured: dict[str, Any] = {}
    child_id = uuid.uuid4()
    job_id = uuid.uuid4()
    captured["child_id"] = child_id
    captured["job_id"] = job_id

    async def fake_create_build(
        conn: Any, project: str, *, parent_build_id: Any = None, **kw: Any
    ) -> uuid.UUID:
        captured["create_build_parent"] = parent_build_id
        return child_id

    async def fake_clone(conn: Any, project: str, parent: Any, child: Any) -> CloneCounts:
        captured["clone"] = (parent, child)
        return CloneCounts(documents=2)

    async def fake_create_job(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        captured["kind"] = kind
        captured["job_build_id"] = build_id
        from types import SimpleNamespace

        return SimpleNamespace(id=job_id, status="queued")

    async def fake_enqueue(redis: Any, project: str, jid: Any) -> bool:
        captured["enqueued_job_id"] = jid
        return True

    monkeypatch.setattr("api.routers.builds.create_build", fake_create_build)
    monkeypatch.setattr("api.routers.builds.clone_raw_artifacts", fake_clone)
    monkeypatch.setattr("api.routers.builds.create_job_exclusive", fake_create_job)
    monkeypatch.setattr("api.routers.builds.enqueue_build", fake_enqueue)
    return captured


def test_retry_returns_202_and_job_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_parent(monkeypatch)
    _stub_produce(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "queued" and "job_id" in data


def test_retry_records_child_lineage_and_retry_kind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_parent(monkeypatch)
    captured = _stub_produce(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    # the child records the PARENT it retried (lineage, never a parent edit)...
    assert captured["create_build_parent"] == _PARENT
    # ...the raw layer is cloned parent → child...
    assert captured["clone"] == (_PARENT, captured["child_id"])
    # ...and the job is a 'retry' bound to the CHILD build (what the worker
    # resumes — a job bound to the PARENT would resume the failed build in place)
    assert captured["kind"] == "retry"
    assert captured["job_build_id"] == captured["child_id"]
    assert captured["enqueued_job_id"] == captured["job_id"]


@pytest.mark.parametrize("status", ["building", "ready", "active", "archived"])
def test_retry_non_failed_build_is_409_not_retryable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # only a TERMINAL failed build is retryable — retrying a building/ready/
    # active/archived build would fork a live or vetted snapshot. The guard runs
    # BEFORE any child is created (stub produce so a leak would 202, not 409).
    _stub_parent(monkeypatch, status=status)
    _stub_produce(monkeypatch)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "BUILD_NOT_RETRYABLE")
    assert resp.json()["error"]["details"]["status"] == status


def test_retry_with_no_reusable_documents_is_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a parent that failed AT/BEFORE ingest committed 0 documents. Because a
    # retry child SKIPS live-source ingest, letting it through would run the
    # pipeline on an EMPTY corpus and reach 'ready' — masking the ingest failure
    # (Codex #100 P1 R2). The endpoint must refuse when the clone reused nothing.
    _stub_parent(monkeypatch)
    captured = _stub_produce(monkeypatch)

    async def empty_clone(conn: Any, project: str, parent: Any, child: Any) -> CloneCounts:
        return CloneCounts(documents=0)

    monkeypatch.setattr("api.routers.builds.clone_raw_artifacts", empty_clone)

    # the job must NEVER be created for a no-document retry
    def _fail_job(*a: Any, **kw: Any) -> Any:
        raise AssertionError("create_job_exclusive must not run for a 0-document retry")

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", _fail_job)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "BUILD_NOT_RETRYABLE")
    assert resp.json()["error"]["details"]["documents"] == 0
    assert "enqueued_job_id" not in captured  # nothing dispatched


def test_retry_unknown_project_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_project(conn: object, name: str, *, for_update: bool = False) -> None:
        return None

    monkeypatch.setattr("api.routers.builds.get_project", no_project)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "PROJECT_NOT_FOUND")


def test_retry_unknown_build_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def a_project(conn: object, name: str, *, for_update: bool = False) -> object:
        return object()

    async def no_build(conn: object, project: str, build_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr("api.routers.builds.get_project", a_project)
    monkeypatch.setattr("api.routers.builds.get_build_info", no_build)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "BUILD_NOT_FOUND")


def test_retry_overlapping_job_is_409_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_parent(monkeypatch)
    _stub_produce(monkeypatch)

    async def conflict(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise JobConflictError(project, uuid.uuid4())

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", conflict)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "JOB_CONFLICT")


def test_retry_project_vanished_at_job_creation_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the build 404 gate passed, but a concurrent delete_project can win the
    # projects-row lock create_job_exclusive takes — that raises ProjectNotFound,
    # which the endpoint maps to 404 (not a 500)
    _stub_parent(monkeypatch)
    _stub_produce(monkeypatch)

    async def missing(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise ProjectNotFoundError(project)

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", missing)
    resp = client.post(_URL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "PROJECT_NOT_FOUND")


def test_retry_reason_is_rejected_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the contract declares `reason`, but retry builds have no note column yet —
    # a present reason must fail loud (400), never be silently dropped. The guard
    # is BEFORE the parent read, so no stubs are needed.
    resp = client.post(_URL, json={"reason": "flaky LLM, retrying"})
    assert resp.status_code == 400


def test_retry_null_body_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # #53 R5: a literal JSON `null` body binds to None (indistinguishable from
    # absent), so without the shared guard it would silently 202 while
    # `{"reason": null}` is rejected — the field-null-rejected/whole-body-null-
    # accepted asymmetry. The guard runs before the parent read, so no stubs.
    resp = client.post(_URL, content=b"null", headers={"Content-Type": "application/json"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (400, "VALIDATION_ERROR")


def test_retry_folds_body_into_idempotency_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a reused Idempotency-Key with a DIFFERENT body must not replay the stored
    # job (§27): the request hash folds the raw body, so a genuine duplicate
    # (same body) hashes the same while a different body diverges.
    _stub_parent(monkeypatch)
    seen: list[str] = []

    async def fake_run_idempotent(
        conn: Any, *, req_hash: str, **kw: Any
    ) -> tuple[int, dict[str, Any]]:
        seen.append(req_hash)
        return 202, {"data": {"status": "queued", "job_id": str(uuid.uuid4())}}

    monkeypatch.setattr("api.routers.builds.run_idempotent", fake_run_idempotent)
    headers = {"Idempotency-Key": "k1"}
    # the body must stay a VALID RetryRequest (the endpoint parses it, unlike the
    # bodyless eval endpoint): no body (b"") vs an empty object (b"{}") — both
    # "no reason", but distinct bytes, so the folded hash must diverge
    client.post(_URL, headers=headers)
    client.post(_URL, headers=headers, json={})
    client.post(_URL, headers=headers)
    assert seen[0] != seen[1]  # different body ⇒ different hash (no stale replay)
    assert seen[0] == seen[2]  # a genuine duplicate still hashes the same
