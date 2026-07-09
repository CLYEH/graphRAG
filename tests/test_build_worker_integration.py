"""Why: the component tests pin the worker's wiring, but only a real Redis
round trip proves the arq layer itself — that an enqueued job is dequeued and
executed by a running worker, its startup builds usable deps, and the build
lands `done`. This drives a real arq burst worker against live Redis + Postgres
with the LLM/embedder + stages faked (so no API key and no real pipeline is
needed — the point under test is the queue→worker→job_lease→run_build path, not
the model), asserting the job reaches `done`, exactly one build was created, and
the execution lease was released.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from arq import create_pool
from arq.worker import Worker
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from api.workers import build_worker as bw
from core.builds.orchestrator import _STAGE_ORDER, StageResult, Stages
from core.config import get_settings
from core.registry import create_job, create_project, get_job
from core.stores import tables

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_StageFn = Callable[[AsyncConnection, str, uuid.UUID], Awaitable[StageResult]]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


def _noop_stages() -> Stages:
    def _stage() -> _StageFn:
        async def stage(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
            return StageResult()

        return stage

    return Stages(**{n: _stage() for n in _STAGE_ORDER})


async def _cleanup(engine: AsyncEngine, project: str) -> None:
    async with engine.connect() as conn, conn.begin():
        run_ids = sa.select(tables.pipeline_runs.c.id).where(
            tables.pipeline_runs.c.project == project
        )
        step_ids = sa.select(tables.pipeline_steps.c.id).where(
            tables.pipeline_steps.c.run_id.in_(run_ids)
        )
        await conn.execute(
            tables.pipeline_step_items.delete().where(
                tables.pipeline_step_items.c.step_id.in_(step_ids)
            )
        )
        await conn.execute(
            tables.pipeline_steps.delete().where(tables.pipeline_steps.c.run_id.in_(run_ids))
        )
        for table in (tables.pipeline_runs, tables.builds, tables.jobs):
            await conn.execute(table.delete().where(table.c.project == project))
        await conn.execute(tables.projects.delete().where(tables.projects.c.name == project))
    await engine.dispose()


async def test_worker_executes_an_enqueued_build(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fake the model factories (no API key) + the stages (noop) so this exercises
    # the REAL enqueue→dequeue→execute round trip + real Postgres, hermetically.
    monkeypatch.setattr(bw, "chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(bw, "embedding_model", lambda: SimpleNamespace())
    monkeypatch.setattr(bw, "default_stages", lambda config, **deps: _noop_stages())

    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")

        pool = await create_pool(bw._redis_settings())
        await bw.enqueue_build(pool, project, job.id)

        worker = Worker(
            functions=[bw.run_build_task],
            on_startup=bw.on_startup,
            on_shutdown=bw.on_shutdown,
            redis_settings=bw._redis_settings(),
            burst=True,  # drain the queue then stop
            handle_signals=False,  # we're not the main process
            poll_delay=0.1,
            keep_result=0,
        )
        try:
            await worker.main()
        finally:
            # arq's Worker.close() sends signal.SIGUSR1 when handle_signals=False
            # (Unix-only) BEFORE its real teardown; flip the flag so close() skips
            # that line and still runs the rest (gather tasks → on_shutdown → close
            # pool) cross-platform.
            worker._handle_signals = True
            await worker.close()
        await pool.aclose()

        # the worker ran the build to completion and released its lease.
        async with engine.connect() as conn:
            job_row = await get_job(conn, job.id)
            builds = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.builds)
                    .where(tables.builds.c.project == project)
                )
            ).scalar_one()
            lease_owner = (
                await conn.execute(
                    sa.select(tables.jobs.c.lease_owner).where(tables.jobs.c.id == job.id)
                )
            ).scalar_one()
        assert job_row is not None and job_row.status == "done"
        assert builds == 1
        assert lease_owner is None
    finally:
        await _cleanup(engine, project)
