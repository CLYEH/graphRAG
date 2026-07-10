"""Why: BA2e-1's routers own the HTTP behaviors above the registry — the 202
JobAccepted / Job envelopes, the frozen-full-shape Job serialization (nullable
fields null, never absent — §27.2), the domain→frozen-code mappings (JOB_NOT_
FOUND / JOB_CONFLICT), the trigger's create→enqueue ORDER (a 409/404 must never
enqueue), and the loud rejection of contract fields the pipeline cannot honor
yet (source_ids / reason — owner decision 2026-07-10). These hold without
Postgres or Redis: registry + enqueue are stubbed, the real app supplies
middleware, validation, and exception handlers. Live SQL/idempotency behavior
is the integration suite's job.
"""

from __future__ import annotations

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
from api.deps import arq_redis_provider, db_conn
from core.registry import Job, JobConflictError, JobNotFoundError, ProjectNotFoundError

pytestmark = pytest.mark.contract

_TS = datetime(2026, 7, 10, tzinfo=UTC)


def _job(**over: Any) -> Job:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "kind": "build",
        "build_id": None,
        "status": "queued",
        "step": None,
        "progress": 0.0,
        "message": None,
        "error": None,
        "cancel_requested": False,
        "created_at": _TS,
        "finished_at": None,
    }
    base.update(over)
    return Job(**base)


@pytest.fixture()
def queue_touches() -> list[int]:
    """Each element = one lazy-pool acquisition (a get_redis() call)."""
    return []


@pytest.fixture()
def client(queue_touches: list[int]) -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()  # registry is stubbed; the connection is never used

    def _provider() -> Any:
        async def _get() -> object:
            queue_touches.append(1)
            return object()

        return _get

    app.dependency_overrides[db_conn] = _conn
    # the enqueue helper is stubbed too — but the provider dependency must be
    # overridden or an enqueue path would lazily open a REAL Redis pool; the
    # fake counts acquisitions so tests can pin that non-enqueue responses
    # never touch the queue (Codex round 3)
    app.dependency_overrides[arq_redis_provider] = _provider
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, module: str, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.{module}.{name}", fn)


def _spy_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, uuid.UUID]]:
    calls: list[tuple[str, uuid.UUID]] = []

    async def fake_enqueue(redis: Any, project: str, job_id: uuid.UUID) -> bool:
        calls.append((project, job_id))
        return True

    _stub(monkeypatch, "triggers", "enqueue_build", fake_enqueue)
    return calls


# ── GET /jobs/{id} ───────────────────────────────────────────────────────────


def test_get_job_serves_the_full_frozen_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: §27.2 — nullable fields are null, never ABSENT, so consumers never
    # branch on missing keys; and the internal columns must never leak.
    job = _job()

    async def fake_get(conn: Any, job_id: uuid.UUID) -> Job:
        return job

    _stub(monkeypatch, "jobs", "get_job", fake_get)
    r = client.get(f"/jobs/{job.id}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {
        "job_id",
        "status",
        "kind",
        "project",
        "build_id",
        "step",
        "progress",
        "message",
        "error",
        "created_at",
        "finished_at",
    }
    assert data["job_id"] == str(job.id)
    assert data["status"] == "queued"
    assert data["kind"] == "build"
    assert data["step"] is None and data["message"] is None and data["error"] is None
    assert r.json()["meta"]["build_id"] is None  # no build served this request


def test_get_job_404_wears_the_frozen_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(conn: Any, job_id: uuid.UUID) -> None:
        return None

    _stub(monkeypatch, "jobs", "get_job", fake_get)
    jid = uuid.uuid4()
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert r.json()["error"]["details"]["job_id"] == str(jid)


def test_get_job_malformed_uuid_is_a_validation_error(client: TestClient) -> None:
    # the contract types JobIdPath as uuid — a malformed id is the client's
    # error (400), never a 404 that implies a lookup ran
    r = client.get("/jobs/not-a-uuid")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# ── POST /jobs/{id}/cancel ───────────────────────────────────────────────────


def test_cancel_returns_202_with_current_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(status="running", cancel_requested=True)

    async def fake_cancel(conn: Any, job_id: uuid.UUID) -> Job:
        return job

    _stub(monkeypatch, "jobs", "request_cancel", fake_cancel)
    r = client.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 202
    # the JobAccepted payload — exactly {job_id, status}, status is CURRENT
    assert r.json()["data"] == {"job_id": str(job.id), "status": "running"}


def test_cancel_terminal_job_replays_terminal_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: request_cancel leaves a finished job untouched and returns its
    # current state — the router must surface that as a 202 no-op, not an
    # error (a retried cancel that lost a race to completion is normal).
    job = _job(status="done", finished_at=_TS)

    async def fake_cancel(conn: Any, job_id: uuid.UUID) -> Job:
        return job

    _stub(monkeypatch, "jobs", "request_cancel", fake_cancel)
    r = client.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 202
    assert r.json()["data"]["status"] == "done"


def test_cancel_unknown_job_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_cancel(conn: Any, job_id: uuid.UUID) -> Job:
        raise JobNotFoundError(job_id)

    _stub(monkeypatch, "jobs", "request_cancel", fake_cancel)
    r = client.post(f"/jobs/{uuid.uuid4()}/cancel")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"


# ── POST /projects/{p}/ingest|build (triggers) ──────────────────────────────


def test_trigger_build_202_creates_then_enqueues(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, queue_touches: list[int]
) -> None:
    job = _job(kind="build")
    created: list[tuple[str, str]] = []

    async def fake_create(conn: Any, project: str, kind: str) -> Job:
        created.append((project, kind))
        return job

    _stub(monkeypatch, "triggers", "create_job_exclusive", fake_create)
    enqueued = _spy_enqueue(monkeypatch)

    r = client.post("/projects/p/build")
    assert r.status_code == 202
    assert r.json()["data"] == {"job_id": str(job.id), "status": "queued"}
    assert created == [("p", "build")]
    assert enqueued == [("p", job.id)]  # enqueue rides IN the request, not after
    assert len(queue_touches) == 1  # the pool opens exactly at the enqueue point


def test_trigger_ingest_records_the_ingest_kind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    async def fake_create(conn: Any, project: str, kind: str) -> Job:
        created.append(kind)
        return _job(kind=kind)

    _stub(monkeypatch, "triggers", "create_job_exclusive", fake_create)
    _spy_enqueue(monkeypatch)

    assert client.post("/projects/p/ingest", json={}).status_code == 202
    assert created == ["ingest"]


def test_trigger_conflict_409_and_never_enqueues(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, queue_touches: list[int]
) -> None:
    # WHY: an overlapping job must not enqueue anything — a 409 that still
    # dispatched would run a build the client was told did not start. And the
    # queue must not even be TOUCHED (Codex round 3): a 409 must be servable
    # with Redis unreachable.
    active = uuid.uuid4()

    async def fake_create(conn: Any, project: str, kind: str) -> Job:
        raise JobConflictError(project, active)

    _stub(monkeypatch, "triggers", "create_job_exclusive", fake_create)
    enqueued = _spy_enqueue(monkeypatch)

    r = client.post("/projects/p/build")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_CONFLICT"
    assert r.json()["error"]["details"]["active_job_id"] == str(active)
    assert enqueued == []
    assert queue_touches == []  # the pool was never opened


def test_trigger_unknown_project_404_and_never_enqueues(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, queue_touches: list[int]
) -> None:
    async def fake_create(conn: Any, project: str, kind: str) -> Job:
        raise ProjectNotFoundError(project)

    _stub(monkeypatch, "triggers", "create_job_exclusive", fake_create)
    enqueued = _spy_enqueue(monkeypatch)

    r = client.post("/projects/nope/ingest")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert enqueued == []
    assert queue_touches == []  # a 404 must be servable with Redis unreachable


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/projects/p/ingest", {"source_ids": [str(uuid.uuid4())]}),
        ("/projects/p/ingest", {"source_ids": []}),
        ("/projects/p/ingest", {"source_ids": None}),
        ("/projects/p/build", {"reason": "operator note"}),
        ("/projects/p/build", {"reason": None}),
    ],
)
def test_trigger_rejects_unsupported_fields_loudly(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
    queue_touches: list[int],
) -> None:
    # WHY (owner decision 2026-07-10): the pipeline cannot honor these fields
    # yet — a 202 that then ran a FULL ingest against an explicit restriction,
    # or dropped the operator's note, would silently disobey the request. The
    # field being PRESENT (even null) rejects; nothing is created or enqueued.
    async def fail_create(conn: Any, project: str, kind: str) -> Job:
        raise AssertionError("handler must not run for a rejected body")

    _stub(monkeypatch, "triggers", "create_job_exclusive", fail_create)
    enqueued = _spy_enqueue(monkeypatch)

    r = client.post(path, json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert enqueued == []
    assert queue_touches == []  # a 400 must be servable with Redis unreachable


@pytest.mark.parametrize("body", [None, {}])
def test_trigger_accepts_empty_or_absent_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | None
) -> None:
    async def fake_create(conn: Any, project: str, kind: str) -> Job:
        return _job(kind=kind)

    _stub(monkeypatch, "triggers", "create_job_exclusive", fake_create)
    _spy_enqueue(monkeypatch)

    r = (
        client.post("/projects/p/build", json=body)
        if body is not None
        else client.post("/projects/p/build")
    )
    assert r.status_code == 202


@pytest.mark.parametrize("path", ["/projects/p/ingest", "/projects/p/build"])
def test_trigger_rejects_an_explicit_null_body(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    queue_touches: list[int],
) -> None:
    # WHY (Codex round 5): the contract's requestBody is optional, but when
    # present it is the NON-NULLABLE request object — FastAPI binds a JSON
    # `null` body to None, indistinguishable from absent, which would silently
    # start work for a contract-invalid request. Same strictness as the
    # field-level null rejections.
    async def fail_create(conn: Any, project: str, kind: str) -> Job:
        raise AssertionError("handler must not run for a null body")

    _stub(monkeypatch, "triggers", "create_job_exclusive", fail_create)
    enqueued = _spy_enqueue(monkeypatch)

    r = client.post(path, content=b" null ", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert enqueued == [] and queue_touches == []


def test_trigger_unknown_body_field_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spy_enqueue(monkeypatch)
    r = client.post("/projects/p/ingest", json={"sources": ["x"]})  # typo'd field
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# ── GET /jobs/{id}/events (SSE, BA2e-2) ─────────────────────────────────────


def _script_poller(client: TestClient, frames: Sequence[tuple[Job, datetime] | None]) -> None:
    """Override the stream's SoR seam with scripted observations, consumed one
    per poll (the endpoint's 404 precheck consumes the first); an exhausted
    script observes the row as vanished."""
    from api.routers.jobs import job_poller

    it = iter(frames)

    async def _poll(job_id: uuid.UUID) -> tuple[Job, datetime] | None:
        try:
            return next(it)
        except StopIteration:
            return None

    cast("FastAPI", client.app).dependency_overrides[job_poller] = lambda: _poll


def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        "jobs",
        "get_settings",
        lambda: SimpleNamespace(sse_poll_interval_seconds=0.001),
    )


def _sse_frames(client: TestClient, url: str) -> list[tuple[str, dict[str, Any]]]:
    """GET the stream to completion and parse (event, data) frames."""
    with client.stream("GET", url) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    frames = []
    for block in body.strip().split("\n\n"):
        event_line, data_line = block.split("\n")
        frames.append(
            (event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: ")))
        )
    return frames


def test_sse_emits_updates_on_change_then_terminal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY §27.2: the stream is the Console's live progress feed — the initial
    # state arrives immediately, an unchanged poll emits NOTHING (a fresh
    # clock alone is not progress), every frame carries the FULL frozen shape
    # (step/message null, never absent), and the terminal event ends the
    # stream exactly once.
    _fast_poll(monkeypatch)
    job = _job()
    running = _job(id=job.id, status="running", step="ingest", progress=0.2)
    later = _job(id=job.id, status="running", step="ingest", progress=0.7)
    done = _job(id=job.id, status="done", progress=1.0, message="build ready", finished_at=_TS)
    _script_poller(
        client,
        [(job, _TS), (running, _TS), (running, _TS), (later, _TS), (done, _TS)],
    )

    frames = _sse_frames(client, f"/jobs/{job.id}/events")
    assert [e for e, _ in frames] == ["job.update", "job.update", "job.update", "job.done"]
    for _, data in frames:
        assert set(data) == {"job_id", "status", "step", "progress", "message", "ts"}
        assert data["job_id"] == str(job.id)
    assert frames[0][1]["status"] == "queued" and frames[0][1]["step"] is None
    assert frames[1][1]["progress"] == pytest.approx(0.2)  # the unchanged poll emitted nothing
    assert frames[2][1]["progress"] == pytest.approx(0.7)
    assert frames[3][1]["message"] == "build ready"


def test_sse_terminal_at_connect_emits_exactly_the_terminal_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a late subscriber to a finished job gets its closure, not silence
    _fast_poll(monkeypatch)
    done = _job(status="done", progress=1.0, finished_at=_TS)
    _script_poller(client, [(done, _TS)])
    frames = _sse_frames(client, f"/jobs/{done.id}/events")
    assert [e for e, _ in frames] == ["job.done"]


def test_sse_cancelled_maps_to_job_failed_with_exact_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the frozen event vocabulary has no job.cancelled — a cancelled job
    # is failure-flavored terminal (§14 build precedent), and the frame's
    # status field still carries the exact 'cancelled'.
    _fast_poll(monkeypatch)
    cancelled = _job(status="cancelled", finished_at=_TS)
    _script_poller(client, [(cancelled, _TS)])
    frames = _sse_frames(client, f"/jobs/{cancelled.id}/events")
    assert frames == [
        (
            "job.failed",
            {
                "job_id": str(cancelled.id),
                "status": "cancelled",
                "step": None,
                "progress": 0.0,
                "message": None,
                "ts": _TS.isoformat(),
            },
        )
    ]


def test_sse_unknown_job_is_the_enveloped_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_poll(monkeypatch)
    _script_poller(client, [None])
    jid = uuid.uuid4()
    r = client.get(f"/jobs/{jid}/events")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_sse_vanished_row_ends_stream_without_a_fabricated_terminal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: a job's row can legally vanish mid-stream (terminal job → project
    # CASCADE) — the SoR never held a terminal state this stream observed, so
    # inventing job.failed would lie; the stream just ends and a reconnect
    # gets an honest 404.
    _fast_poll(monkeypatch)
    job = _job()
    _script_poller(client, [(job, _TS)])  # exhausted after the first poll → vanished
    frames = _sse_frames(client, f"/jobs/{job.id}/events")
    assert [e for e, _ in frames] == ["job.update"]  # no terminal frame


# ── DTO shapes ───────────────────────────────────────────────────────────────


def test_job_dtos_project_the_contract_shapes() -> None:
    from api.schemas import job_accepted_dto, job_dto

    # the stored error is the FULL frozen Error (writers mint request_id) and
    # the dto passes it through UNTOUCHED — no re-stamping, no field loss
    stored_error = {
        "code": "INTERNAL",
        "message": "m",
        "details": None,
        "request_id": str(uuid.uuid4()),
    }
    job = _job(status="failed", error=stored_error)
    dto = job_dto(job)
    assert dto["job_id"] == job.id  # id → the contract's job_id
    assert dto["error"] == stored_error
    # internal columns never leak into the frozen shape
    assert "cancel_requested" not in dto and "id" not in dto
    assert job_accepted_dto(job) == {"job_id": job.id, "status": "failed"}
