"""Why: the retry endpoint composes a child build + a document clone + a job
insert into the REQUEST's one transaction (RB1-retry-core). Two properties only
hold end-to-end against live Postgres: (1) a 202 leaves a durable ``building``
child recording ``parent_build_id``, the parent's documents cloned into it, a
``retry`` job bound to the CHILD, one enqueue — and the parent's terminal record
untouched (audit integrity); (2) a JOB_CONFLICT rolls the child + clone back
ATOMICALLY — no orphan ``building`` build stranded without a job, nothing
enqueued. Component tests fake the seams; only real SQL proves the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.deps import arq_redis_provider, db_conn
from core.config import get_settings
from core.registry import create_job_exclusive, get_job
from core.stores import tables

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

#: (client, conn, enqueued spy)
Api = tuple[AsyncClient, AsyncConnection, list[tuple[str, uuid.UUID]]]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


@pytest.fixture()
async def api(migrated: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(dsn, poolclass=NullPool)
    conn = await engine.connect()
    outer = await conn.begin()
    app = create_app()

    async def _override() -> AsyncIterator[AsyncConnection]:
        async with conn.begin_nested():
            yield conn

    enqueued: list[tuple[str, uuid.UUID]] = []

    async def _spy_enqueue(redis: Any, project: str, job_id: uuid.UUID) -> bool:
        enqueued.append((project, job_id))
        return True

    def _provider() -> Any:
        async def _get() -> object:
            return object()

        return _get

    app.dependency_overrides[db_conn] = _override
    app.dependency_overrides[arq_redis_provider] = _provider
    monkeypatch.setattr("api.routers.builds.enqueue_build", _spy_enqueue)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, conn, enqueued
    finally:
        await outer.rollback()
        await conn.close()
        await engine.dispose()


async def _make_project(client: AsyncClient) -> str:
    name = f"retry-it-{uuid.uuid4().hex[:10]}"
    assert (await client.post("/projects", json={"name": name})).status_code == 201
    return name


async def _failed_build_with_docs(conn: AsyncConnection, project: str, docs: int) -> uuid.UUID:
    build_id = cast(
        "uuid.UUID",
        (
            await conn.execute(
                tables.builds.insert()
                .values(project=project, status="failed")
                .returning(tables.builds.c.id)
            )
        ).scalar_one(),
    )
    for i in range(docs):
        await conn.execute(
            tables.documents.insert().values(
                project=project,
                build_id=build_id,
                source_uri=f"file:///doc{i}.txt",
                raw=f"body {i}",
                content_hash=f"hash-{i}",
                mime="text/plain",
                status="ingested",
            )
        )
    return build_id


async def _building_children(conn: AsyncConnection, project: str, parent: uuid.UUID) -> list[Any]:
    return list(
        (
            await conn.execute(
                tables.builds.select().where(
                    tables.builds.c.project == project,
                    tables.builds.c.parent_build_id == parent,
                )
            )
        ).all()
    )


async def test_retry_creates_lineaged_child_clones_docs_and_leaves_parent(api: Api) -> None:
    client, conn, enqueued = api
    project = await _make_project(client)
    parent = await _failed_build_with_docs(conn, project, docs=2)

    r = await client.post(f"/projects/{project}/builds/{parent}/retry")
    assert r.status_code == 202
    data = r.json()["data"]
    assert set(data) == {"job_id", "status"} and data["status"] == "queued"
    job_id = uuid.UUID(data["job_id"])

    # a durable 'building' child recording the parent it retried
    children = await _building_children(conn, project, parent)
    assert len(children) == 1
    child = children[0]
    assert child.status == "building"
    # the parent's documents are cloned into the child (fresh ids, child build)
    child_docs = (
        await conn.execute(tables.documents.select().where(tables.documents.c.build_id == child.id))
    ).all()
    assert {d.content_hash for d in child_docs} == {"hash-0", "hash-1"}
    # the job is a 'retry' bound to the CHILD, enqueued exactly once
    job = await get_job(conn, job_id)
    assert job is not None and job.kind == "retry" and job.build_id == child.id
    assert enqueued == [(project, job_id)]

    # the contract Build.parent_build_id (v1.3) is surfaced on GET: the child
    # points at the parent, an ordinary build is null
    got_child = (await client.get(f"/projects/{project}/builds/{child.id}")).json()["data"]
    assert got_child["parent_build_id"] == str(parent)
    got_parent = (await client.get(f"/projects/{project}/builds/{parent}")).json()["data"]
    assert got_parent["parent_build_id"] is None

    # the parent's terminal record is NEVER mutated (audit integrity)
    parent_row = (
        await conn.execute(tables.builds.select().where(tables.builds.c.id == parent))
    ).one()
    assert parent_row.status == "failed" and parent_row.parent_build_id is None
    parent_docs = (
        await conn.execute(tables.documents.select().where(tables.documents.c.build_id == parent))
    ).all()
    assert len(parent_docs) == 2


async def test_retry_with_no_documents_is_refused_and_leaves_no_child(api: Api) -> None:
    client, conn, enqueued = api
    project = await _make_project(client)
    # a parent that failed AT/BEFORE ingest committed 0 documents
    parent = await _failed_build_with_docs(conn, project, docs=0)

    r = await client.post(f"/projects/{project}/builds/{parent}/retry")
    # nothing to reuse, and a retry skips live ingest → an empty 'ready' build is
    # the failure mode; the endpoint refuses (Codex #100 P1 R2) and rolls back
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BUILD_NOT_RETRYABLE")
    assert r.json()["error"]["details"]["documents"] == 0
    assert await _building_children(conn, project, parent) == []  # no orphan child
    assert enqueued == []


async def test_retry_job_conflict_rolls_back_the_child_atomically(api: Api) -> None:
    client, conn, enqueued = api
    project = await _make_project(client)
    parent = await _failed_build_with_docs(conn, project, docs=1)
    # an overlapping active job already holds the project (the single-active guard)
    await create_job_exclusive(conn, project, "build")

    r = await client.post(f"/projects/{project}/builds/{parent}/retry")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "JOB_CONFLICT")

    # the whole produce() rolled back: NO orphan 'building' child was left behind
    # (a child created before create_job_exclusive raised would strand, since the
    # RESTRICT FK blocks deleting it with the project), and nothing was enqueued
    assert await _building_children(conn, project, parent) == []
    assert enqueued == []
