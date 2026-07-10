"""Why: BA2e-1's trigger/job endpoints are the Console's control surface over
long operations, so their behaviors must hold end-to-end against live Postgres —
the single-active-job 409 (and that it clears once the job terminalizes), the
§27 idempotency replay returning the SAME job (one row, one enqueue) vs the
different-request 409, a failed trigger never poisoning its key, and cancel's
cooperative no-op semantics on terminal jobs. The arq enqueue is spied (Redis is
not part of what these prove; the worker suite owns it) — what matters here is
that a 202 enqueued exactly once and a 4xx enqueued nothing. Each request runs
in a savepoint inside one outer transaction rolled back at teardown, so nothing
lands in the dev DB.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.deps import arq_redis_provider, db_conn
from core.config import get_settings
from core.registry import get_job, set_progress
from core.stores.tables import idempotency_keys

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

#: (client, conn, enqueued spy, queue touches — one element per lazy pool acquisition)
Api = tuple[AsyncClient, AsyncConnection, list[tuple[str, uuid.UUID]], list[int]]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


@pytest.fixture()
async def api(migrated: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    """create_app() over a savepoint-per-request connection (outer transaction
    rolled back at teardown), with the trigger's enqueue spied — no Redis."""
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(dsn, poolclass=NullPool)
    conn = await engine.connect()
    outer = await conn.begin()
    app = create_app()

    async def _override() -> AsyncIterator[AsyncConnection]:
        async with conn.begin_nested():
            yield conn

    enqueued: list[tuple[str, uuid.UUID]] = []
    queue_touches: list[int] = []

    async def _spy_enqueue(redis: Any, project: str, job_id: uuid.UUID) -> bool:
        enqueued.append((project, job_id))
        return True

    def _provider() -> Any:
        async def _get() -> object:
            queue_touches.append(1)
            return object()

        return _get

    app.dependency_overrides[db_conn] = _override
    app.dependency_overrides[arq_redis_provider] = _provider
    monkeypatch.setattr("api.routers.triggers.enqueue_build", _spy_enqueue)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, conn, enqueued, queue_touches
    finally:
        await outer.rollback()
        await conn.close()
        await engine.dispose()


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def _make_project(client: AsyncClient) -> str:
    name = _proj()
    assert (await client.post("/projects", json={"name": name})).status_code == 201
    return name


async def test_trigger_build_creates_job_and_enqueues_once(api: Api) -> None:
    client, conn, enqueued, *_ = api
    project = await _make_project(client)

    r = await client.post(f"/projects/{project}/build")
    assert r.status_code == 202
    data = r.json()["data"]
    assert set(data) == {"job_id", "status"}  # the JobAccepted payload, exactly
    assert data["status"] == "queued"
    assert r.json()["meta"]["build_id"] is None

    job_id = uuid.UUID(data["job_id"])
    job = await get_job(conn, job_id)
    assert job is not None and job.kind == "build" and job.status == "queued"
    assert enqueued == [(project, job_id)]  # exactly one dispatch, in-band

    # GET serves the durable row in the FULL frozen shape
    r = await client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    got = r.json()["data"]
    assert got["job_id"] == str(job_id)
    assert got["kind"] == "build" and got["project"] == project
    assert got["step"] is None and got["error"] is None  # null, never absent
    assert got["created_at"] is not None


async def test_trigger_ingest_records_ingest_kind(api: Api) -> None:
    client, conn, *_ = api
    project = await _make_project(client)

    r = await client.post(f"/projects/{project}/ingest", json={})
    assert r.status_code == 202
    job = await get_job(conn, uuid.UUID(r.json()["data"]["job_id"]))
    assert job is not None and job.kind == "ingest"


async def test_second_trigger_conflicts_until_the_job_terminalizes(api: Api) -> None:
    # WHY: the contract's JOB_CONFLICT ("overlapping job") — one active job per
    # project, and the guard must LIFT once the job reaches a terminal state
    # (it guards overlap, not the project forever).
    client, conn, enqueued, *_ = api
    project = await _make_project(client)

    first = await client.post(f"/projects/{project}/build")
    job_id = uuid.UUID(first.json()["data"]["job_id"])

    r = await client.post(f"/projects/{project}/ingest")  # any kind overlaps
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "JOB_CONFLICT"
    assert err["details"]["active_job_id"] == str(job_id)
    assert len(enqueued) == 1  # the refused trigger enqueued nothing

    async with conn.begin_nested():
        await set_progress(conn, job_id, status="done")
    r = await client.post(f"/projects/{project}/ingest")
    assert r.status_code == 202  # terminal job no longer blocks
    assert len(enqueued) == 2


async def test_trigger_idempotency_replays_one_job(api: Api) -> None:
    # WHY §27: a client retrying a trigger with its key must get the SAME job
    # back — one row, one dispatch — while reusing the key for a DIFFERENT
    # request is a 409, not a silent replay of something else.
    client, conn, enqueued, queue_touches = api
    project = await _make_project(client)
    key = f"k-{uuid.uuid4().hex[:8]}"

    r1 = await client.post(f"/projects/{project}/build", json={}, headers={"Idempotency-Key": key})
    r2 = await client.post(f"/projects/{project}/build", json={}, headers={"Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["data"]["job_id"] == r2.json()["data"]["job_id"]  # replayed, not re-run
    assert len(enqueued) == 1  # the replay did not enqueue a second dispatch
    # ...and never even opened the queue (Codex round 3): the replay must be
    # servable with Redis unreachable, so only the fresh request touched it
    assert len(queue_touches) == 1

    r = await client.post(f"/projects/{project}/ingest", json={}, headers={"Idempotency-Key": key})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_failed_trigger_never_poisons_its_key(api: Api) -> None:
    # WHY §27: the reservation must roll back with the failed request — else
    # the client's retry (after fixing the cause) replays the ERROR forever.
    client, conn, *_ = api
    project = _proj()  # not created yet
    key = f"k-{uuid.uuid4().hex[:8]}"

    r = await client.post(f"/projects/{project}/build", headers={"Idempotency-Key": key})
    assert r.status_code == 404
    stored = await conn.execute(idempotency_keys.select().where(idempotency_keys.c.key == key))
    assert stored.first() is None  # the 404 rolled the reservation back

    assert (await client.post("/projects", json={"name": project})).status_code == 201
    r = await client.post(f"/projects/{project}/build", headers={"Idempotency-Key": key})
    assert r.status_code == 202  # same key, now fresh — not a replayed 404


async def test_cancel_sets_the_cooperative_flag_idempotently(api: Api) -> None:
    client, conn, *_ = api
    project = await _make_project(client)
    job_id = uuid.UUID((await client.post(f"/projects/{project}/build")).json()["data"]["job_id"])

    r = await client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 202
    assert r.json()["data"] == {"job_id": str(job_id), "status": "queued"}  # CURRENT status
    job = await get_job(conn, job_id)
    assert job is not None and job.cancel_requested is True
    assert job.status == "queued"  # cooperative: the worker stops it, not the API

    assert (await client.post(f"/jobs/{job_id}/cancel")).status_code == 202  # re-cancel no-op


async def test_cancel_terminal_job_is_a_noop_not_an_error(api: Api) -> None:
    client, conn, *_ = api
    project = await _make_project(client)
    job_id = uuid.UUID((await client.post(f"/projects/{project}/build")).json()["data"]["job_id"])
    async with conn.begin_nested():
        await set_progress(conn, job_id, status="done")

    r = await client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 202
    assert r.json()["data"]["status"] == "done"  # replays the terminal state
    job = await get_job(conn, job_id)
    assert job is not None and job.cancel_requested is False  # a finished job is untouched


async def test_cancel_replays_its_stored_response_after_the_job_row_vanishes(api: Api) -> None:
    # WHY §27 (Codex round 2): the replay guarantee outlives the job row — a
    # terminal job CASCADE-deletes with its project, and the client's retry
    # with the same key must get its stored 202 back (or the conflict on a
    # different request), NEVER a JOB_NOT_FOUND that implies the cancel was
    # not accepted. The replay peek must therefore run before the 404 precheck.
    client, conn, *_ = api
    project = await _make_project(client)
    job_id = uuid.UUID((await client.post(f"/projects/{project}/build")).json()["data"]["job_id"])
    other_job = uuid.UUID(
        (await client.post(f"/projects/{await _make_project(client)}/build")).json()["data"][
            "job_id"
        ]
    )
    async with conn.begin_nested():
        await set_progress(conn, job_id, status="done")  # terminal → delete no longer blocked
    key = f"k-{uuid.uuid4().hex[:8]}"

    first = await client.post(f"/jobs/{job_id}/cancel", headers={"Idempotency-Key": key})
    assert first.status_code == 202 and first.json()["data"]["status"] == "done"

    assert (await client.delete(f"/projects/{project}")).status_code == 204
    assert (await client.get(f"/jobs/{job_id}")).status_code == 404  # the row is gone

    replayed = await client.post(f"/jobs/{job_id}/cancel", headers={"Idempotency-Key": key})
    assert replayed.status_code == 202  # replayed verbatim, not JOB_NOT_FOUND
    assert replayed.json() == first.json()

    # reusing the key for a DIFFERENT request (another job's cancel) is still
    # the idempotency conflict — not a 404 from the vanished-row precheck
    r = await client.post(f"/jobs/{other_job}/cancel", headers={"Idempotency-Key": key})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_job_endpoints_404_on_unknown_job(api: Api) -> None:
    client, conn, *_ = api
    jid = uuid.uuid4()
    assert (await client.get(f"/jobs/{jid}")).status_code == 404
    r = await client.post(f"/jobs/{jid}/cancel", headers={"Idempotency-Key": "k-404"})
    assert r.status_code == 404
    stored = await conn.execute(idempotency_keys.select().where(idempotency_keys.c.key == "k-404"))
    assert stored.first() is None  # a 404 cancel never reserves the key
