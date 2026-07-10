"""Why: the SSE stream is the Console's live progress feed and its whole
design is "poll the jobs SoR on short-lived connections" — that must hold
end-to-end against live Postgres: a subscriber connected BEFORE the worker
writes progress must observe the committed updates of a CONCURRENT writer on
another connection (the savepoint-per-request harness can't prove this — its
uncommitted world is invisible to the stream's own connections, so this suite
uses committed rows + cleanup, the lease-test pattern), frames must carry the
frozen §27.2 shape with DB-clock timestamps, and the stream must end exactly
at the terminal event. get_job_at's single-statement row+clock read is pinned
here too (its ts is the DB's, not the API host's).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from core.config import get_settings
from core.registry import create_job, create_project, get_job_at, set_progress
from core.stores.tables import jobs, projects

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def _cleanup(engine: AsyncEngine, project: str) -> None:
    async with engine.connect() as conn:
        await conn.execute(jobs.delete().where(jobs.c.project == project))
        await conn.execute(projects.delete().where(projects.c.name == project))
        await conn.commit()
    await engine.dispose()


def _parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = []
    for block in body.strip().split("\n\n"):
        event_line, data_line = block.split("\n")
        frames.append(
            (event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: ")))
        )
    return frames


async def test_stream_observes_a_concurrent_writers_committed_progress(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    project = _proj()
    monkeypatch.setattr(
        "api.routers.jobs.get_settings",
        lambda: SimpleNamespace(sse_poll_interval_seconds=0.05),
    )
    try:
        async with engine.connect() as conn:
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")
            await conn.commit()  # visible to the stream's own connections

        async def _drive() -> None:
            """The worker: progress in its own committed transactions."""
            driver = _engine()
            try:
                await asyncio.sleep(0.2)
                async with driver.begin() as conn:
                    await set_progress(conn, job.id, status="running", step="ingest", progress=0.3)
                await asyncio.sleep(0.2)
                async with driver.begin() as conn:
                    await set_progress(
                        conn, job.id, status="done", progress=1.0, message="build ready"
                    )
            finally:
                await driver.dispose()

        app = create_app()  # NO overrides: real lifespan engine, real poll seam
        driver = asyncio.create_task(_drive())
        try:
            transport = ASGITransport(app=app)
            # ASGITransport does not drive lifespan — enter it explicitly so
            # app.state.engine (the stream's per-poll connection source) exists
            async with (
                app.router.lifespan_context(app),
                AsyncClient(transport=transport, base_url="http://t") as client,
                client.stream("GET", f"/jobs/{job.id}/events") as r,
            ):
                assert r.status_code == 200
                body = "".join([chunk async for chunk in r.aiter_text()])
        finally:
            await driver

        frames = _parse(body)
        events = [e for e, _ in frames]
        # initial queued state immediately; the concurrent writer's committed
        # running update observed; terminal frame ends the stream
        assert events[0] == "job.update" and frames[0][1]["status"] == "queued"
        assert ("job.update", "running") in [(e, d["status"]) for e, d in frames]
        assert events[-1] == "job.done" and frames[-1][1]["message"] == "build ready"
        for _, data in frames:
            assert set(data) == {"job_id", "status", "step", "progress", "message", "ts"}
        # ts is the DB clock at each observation — parseable and non-decreasing
        stamps = [datetime.fromisoformat(d["ts"]) for _, d in frames]
        assert stamps == sorted(stamps)
    finally:
        await _cleanup(engine, project)


async def test_get_job_at_reads_row_and_db_clock_in_one_statement(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")

            observed = await get_job_at(conn, job.id)
            assert observed is not None
            got, ts = observed
            assert got == job  # the same frozen dataclass row
            assert ts.tzinfo is not None  # timestamptz — the DB's clock, tz-aware
            assert abs((ts - datetime.now(UTC)).total_seconds()) < 60  # sane clock

            assert await get_job_at(conn, uuid.uuid4()) is None  # unknown → None
            await trans.rollback()
    finally:
        await engine.dispose()
