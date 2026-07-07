"""Why: the jobs repo is the control plane the worker (BA2c) and job endpoints
(BA2d) build on, so its behaviors must hold against live Postgres — the JSONB
error round-trip, the partial-update omitted-vs-set semantics the worker relies
on for progress, cooperative-cancel only touching live jobs, the CASCADE FK, and
the delete_project active-jobs guard that stops a project vanishing mid-run.
Fakes can't prove the SQL that enforces these. All work runs in a rolled-back
transaction so nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.registry import (
    ProjectHasActiveJobsError,
    count_active_jobs,
    create_job,
    create_project,
    delete_project,
    get_job,
    is_cancel_requested,
    request_cancel,
    set_progress,
)
from core.registry.jobs import JobNotFoundError
from core.stores.tables import jobs

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


async def test_create_get_and_progress_roundtrip(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)

            job = await create_job(conn, project, "build")
            assert job.status == "queued"  # server default
            assert job.progress == 0.0
            assert job.build_id is None
            assert job.error is None
            assert not job.cancel_requested
            assert await get_job(conn, job.id) == job  # frozen dataclass equality

            build_id = uuid.uuid4()
            updated = await set_progress(
                conn,
                job.id,
                status="running",
                step="graph",
                progress=0.5,
                message="extracting",
                build_id=build_id,
            )
            assert updated is not None
            assert (updated.status, updated.step, updated.progress) == ("running", "graph", 0.5)
            assert updated.build_id == build_id
            # a field NOT passed stays put — message set, error still null
            assert updated.message == "extracting"
            assert updated.error is None

            # error stores the §15 Error shape and round-trips as a dict (not a
            # JSONB 'null' — none_as_null keeps an un-errored job's column SQL NULL)
            errored = await set_progress(
                conn, job.id, status="failed", error={"code": "INTERNAL", "message": "boom"}
            )
            assert errored is not None
            assert errored.error == {"code": "INTERNAL", "message": "boom"}
            assert errored.step == "graph"  # untouched by this patch
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_progress_bounds_and_status_enforced_by_db(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "ingest")
            # the CHECK, not app code, rejects an out-of-range progress...
            with pytest.raises(IntegrityError):
                await set_progress(conn, job.id, progress=1.5)
            await trans.rollback()

            trans = await conn.begin()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "ingest")
            # ...and a status outside the frozen JobStatus enum
            with pytest.raises(IntegrityError):
                await set_progress(conn, job.id, status="succeeded")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_cooperative_cancel_only_flips_live_jobs(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")

            assert not await is_cancel_requested(conn, job.id)
            after = await request_cancel(conn, job.id)
            assert after.cancel_requested
            assert await is_cancel_requested(conn, job.id)

            # a terminal job is left untouched — cancel is a no-op past the finish
            done = await create_job(conn, project, "build")
            await set_progress(conn, done.id, status="done", progress=1.0)
            after_done = await request_cancel(conn, done.id)
            assert not after_done.cancel_requested

            with pytest.raises(JobNotFoundError):
                await request_cancel(conn, uuid.uuid4())
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_request_cancel_update_is_status_guarded_against_a_stale_read(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """request_cancel's decisive check is the UPDATE's ``WHERE status IN active``,
    not just the get_job read before it. The orchestrator's finalize holds the
    job lock and reads cancel_requested under it, so a cancel racing that
    finalize can pass the (unlocked) status read while the job is still 'running'
    yet have its UPDATE land after the job is already 'done' — the guard must
    make that a clean no-op, never flagging a finished job (the limbo Codex
    flagged). We force the exact TOCTOU: feed request_cancel a stale 'running'
    snapshot while the row is really 'done'."""
    import dataclasses

    from core.registry import jobs as jobs_mod

    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")
            await set_progress(conn, job.id, status="done", progress=1.0)  # truly terminal

            stale_running = dataclasses.replace(job, status="running", cancel_requested=False)
            real_get = jobs_mod.get_job
            reads = {"n": 0}

            async def _stale_then_real(c: object, jid: uuid.UUID) -> object:
                reads["n"] += 1
                if reads["n"] == 1:  # the pre-update read request_cancel branches on
                    return stale_running
                return await real_get(c, jid)  # type: ignore[arg-type]

            monkeypatch.setattr(jobs_mod, "get_job", _stale_then_real)
            result = await request_cancel(conn, job.id)

            # the guarded UPDATE matched 0 rows (the row is 'done') → no flag set
            assert result.status == "done"
            assert result.cancel_requested is False
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_delete_project_refuses_while_a_job_is_active(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")  # queued → active

            assert await count_active_jobs(conn, project) == 1
            with pytest.raises(ProjectHasActiveJobsError):
                await delete_project(conn, project)

            # once terminal, the guard lets go and the delete CASCADES the job away
            await set_progress(conn, job.id, status="done", progress=1.0)
            assert await count_active_jobs(conn, project) == 0
            assert await delete_project(conn, project) is True
            assert (
                await conn.execute(sa.select(jobs.c.id).where(jobs.c.id == job.id))
            ).one_or_none() is None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_delete_project_locks_the_row_before_counting_jobs(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execution-level proof the count-then-delete TOCTOU is closed. The race is
    the window BETWEEN the active-jobs count returning 0 and the DELETE: a
    create_job committing there is silently CASCADE-removed. The fix takes
    FOR UPDATE on the projects row BEFORE the count, so a competing
    FOR KEY SHARE (exactly a create_job FK insert's lock) is already blocked
    during that window. We pause inside count_active_jobs (after the lock, before
    the delete) and probe with NOWAIT — without the pre-count lock the row is
    still free there, so this test fails; the DELETE's own lock (which happens
    later) would mask the bug if we probed after delete_project returned."""
    import asyncio

    from sqlalchemy.exc import DBAPIError

    from core.registry import jobs as jobs_mod
    from core.stores.tables import projects

    engine = _engine()
    project = _proj()
    entered = asyncio.Event()
    release = asyncio.Event()
    real_count = jobs_mod.count_active_jobs

    async def paused_count(conn: object, name: str) -> int:
        entered.set()
        await release.wait()
        return await real_count(conn, name)  # type: ignore[arg-type]

    monkeypatch.setattr(jobs_mod, "count_active_jobs", paused_count)
    try:
        async with engine.connect() as setup:
            await create_project(setup, name=project)
            await setup.commit()  # visible to a second connection

        async def _delete() -> None:
            async with engine.connect() as a:
                await a.begin()
                await delete_project(a, project)  # lock → paused count → delete
                await a.rollback()  # restore the project; we only wanted the timing

        deleter = asyncio.create_task(_delete())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5.0)  # A is mid-count, holding the lock
            async with engine.connect() as b:
                await b.begin()
                with pytest.raises(DBAPIError) as ei:
                    await b.execute(
                        sa.select(projects.c.name)
                        .where(projects.c.name == project)
                        .with_for_update(key_share=True, nowait=True)
                    )
                assert getattr(ei.value.orig, "sqlstate", None) == "55P03"  # lock_not_available
                await b.rollback()
        finally:
            release.set()
            await deleter
    finally:
        async with engine.connect() as cleanup:
            monkeypatch.setattr(jobs_mod, "count_active_jobs", real_count)
            await delete_project(cleanup, project)
            await cleanup.commit()
        await engine.dispose()


async def test_job_requires_an_existing_project(migrated: None) -> None:
    """The FK backstops a job for a project that never existed (or vanished)."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):
                await create_job(conn, _proj(), "build")  # no projects row
            await trans.rollback()
    finally:
        await engine.dispose()
