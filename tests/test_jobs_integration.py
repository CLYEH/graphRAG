"""Why: the jobs repo is the control plane the worker (BA2c) and job endpoints
(BA2d) build on, so its behaviors must hold against live Postgres — the JSONB
error round-trip, the partial-update omitted-vs-set semantics the worker relies
on for progress, cooperative-cancel only touching live jobs, the CASCADE FK, and
the delete_project active-jobs guard that stops a project vanishing mid-run.
Fakes can't prove the SQL that enforces these. All work runs in a rolled-back
transaction so nothing lands in the dev DB.
"""

from __future__ import annotations

import asyncio
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
    JobConflictError,
    ProjectHasActiveJobsError,
    ProjectNotFoundError,
    build_config_snapshot,
    count_active_jobs,
    create_job,
    create_job_exclusive,
    create_project,
    delete_project,
    find_unenqueued_jobs,
    get_job,
    is_cancel_requested,
    request_cancel,
    set_progress,
)
from core.registry.jobs import JobNotFoundError, acquire_lease
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


async def test_build_config_snapshot_returns_the_build_job_not_an_eval(migrated: None) -> None:
    """RB1-retry-skip pins the PARENT's config onto the retry child. The lookup
    must return the config the build was BUILT with (its build/retry job's
    snapshot) and EXCLUDE a later eval of the same build, whose snapshot is the
    project config as of the eval — which may have drifted. Discriminating: the
    project config drifts BETWEEN the build job and an eval of the same build, so a
    lookup that didn't exclude eval could return the drifted 9999, not the 1000 the
    build ran."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            build_id = uuid.uuid4()
            # the build's config, then its build job (bound to build_id) snapshots it
            await conn.execute(
                projects.update()
                .where(projects.c.name == project)
                .values(config={"chunking": {"max_chars": 1000}})
            )
            await create_job(conn, project, "build", build_id=build_id)
            # the project config DRIFTS, then an eval of the SAME build snapshots it
            await conn.execute(
                projects.update()
                .where(projects.c.name == project)
                .values(config={"chunking": {"max_chars": 9999}})
            )
            await create_job(conn, project, "eval", build_id=build_id)

            snap = await build_config_snapshot(conn, build_id, ignore_kind="eval")
            assert snap == {"chunking": {"max_chars": 1000}}  # the build's, not eval's 9999
            # a build_id with no producing job → None (the endpoint's live-config fallback)
            assert await build_config_snapshot(conn, uuid.uuid4(), ignore_kind="eval") is None
            await trans.rollback()
    finally:
        await engine.dispose()


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

            # error stores the FULL §15 Error shape (the 0014 CHECK refuses a
            # partial object) and round-trips as a dict (not a JSONB 'null' —
            # none_as_null keeps an un-errored job's column SQL NULL)
            full_error = {
                "code": "INTERNAL",
                "message": "boom",
                "details": None,
                "request_id": str(uuid.uuid4()),
            }
            errored = await set_progress(conn, job.id, status="failed", error=full_error)
            assert errored is not None
            assert errored.error == full_error
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


# ── create_job_exclusive (BA2e-1 trigger guard) ─────────────────────────────


async def test_create_job_exclusive_conflicts_while_a_job_is_active(migrated: None) -> None:
    """WHY: the contract's 409 JOB_CONFLICT — one active job per project. The
    guard must name the blocking job (so a client can join it) and must LIFT
    once that job terminalizes (it guards overlap, not the project forever)."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)

            first = await create_job_exclusive(conn, project, "build")
            with pytest.raises(JobConflictError) as ei:
                await create_job_exclusive(conn, project, "ingest")  # any kind overlaps
            assert ei.value.active_job_id == first.id

            await set_progress(conn, first.id, status="failed")
            second = await create_job_exclusive(conn, project, "ingest")  # terminal → free
            assert second.kind == "ingest"
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_create_job_exclusive_missing_project(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(ProjectNotFoundError):
                await create_job_exclusive(conn, _proj(), "build")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_create_job_exclusive_race_has_one_winner(migrated: None) -> None:
    """WHY (class 10): an app-level count-then-insert would let two concurrent
    triggers both see zero active jobs and both insert. The projects row lock
    must serialize them: the second BLOCKS until the first commits, then sees
    its job and conflicts — never two active jobs."""
    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as seed:
            await create_project(seed, name=project)
            await seed.commit()  # visible to both racing connections

        async with engine.connect() as conn_a, engine.connect() as conn_b:
            txn_a = await conn_a.begin()
            winner = await create_job_exclusive(conn_a, project, "build")  # holds the row lock

            async def _contender() -> None:
                async with conn_b.begin():
                    await create_job_exclusive(conn_b, project, "build")

            contender = asyncio.create_task(_contender())
            await asyncio.sleep(0.3)
            assert not contender.done()  # blocked on the row lock, not failed and not inserted
            await txn_a.commit()
            with pytest.raises(JobConflictError) as ei:
                await contender
            assert ei.value.active_job_id == winner.id
    finally:
        async with engine.connect() as cleanup:
            await cleanup.execute(jobs.delete().where(jobs.c.project == project))
            await cleanup.execute(projects.delete().where(projects.c.name == project))
            await cleanup.commit()
        await engine.dispose()


def test_upgrade_0014_backfills_legacy_partial_errors_before_the_check(
    require_services: None,
) -> None:
    """0014 must apply on a DB that already holds failed jobs written by the
    pre-BA2e-1 writers (error = {code, message, details} only — legal at 0013,
    where no CHECK exists): the backfill runs BEFORE ADD CONSTRAINT, so a
    populated upgrade reconciles instead of failing, and the stored shape
    becomes the full frozen Error — request_id stamped, original fields
    preserved. CI migrates a fresh empty DB, so without this test the
    backfill (the migration's whole reason to exist) would never execute over
    a violating row. Sync test: alembic's env.py drives its own asyncio.run,
    which can't run inside an async test's loop (the 0010 orphan-builds test
    is the precedent)."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    project = f"itest-legacy-{uuid.uuid4().hex[:8]}"
    legacy = {"code": "INTERNAL", "message": "boom", "details": None}  # pre-0014 shape
    job_ids: list[uuid.UUID] = []

    async def _insert_legacy_row() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                await create_project(conn, name=project)
                job = await create_job(conn, project, "build")
                await set_progress(conn, job.id, status="failed", error=legacy)
                job_ids.append(job.id)
                await conn.commit()
        finally:
            await engine.dispose()

    async def _assert_reconciled() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                job = await get_job(conn, job_ids[0])
                assert job is not None and job.error is not None
                assert set(job.error) == {"code", "message", "details", "request_id"}
                assert job.error["code"] == "INTERNAL"  # preserved
                assert job.error["message"] == "boom"  # preserved
                assert job.error["details"] is None  # preserved
                uuid.UUID(job.error["request_id"])  # stamped, parseable
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                await conn.execute(jobs.delete().where(jobs.c.project == project))
                await conn.execute(projects.delete().where(projects.c.name == project))
                await conn.commit()
        finally:
            await engine.dispose()

    try:
        command.downgrade(cfg, "0013_jobs_reaper_index")  # drop the CHECK (+ index)
        asyncio.run(_insert_legacy_row())
        command.upgrade(cfg, "head")  # MUST NOT fail on the legacy row
        asyncio.run(_assert_reconciled())
    finally:
        # robust to either migration state: remove the rows, then ensure head
        asyncio.run(_cleanup())
        command.upgrade(cfg, "head")


async def test_partial_job_error_is_refused_by_the_db(migrated: None) -> None:
    """WHY (0014): GET /jobs/{id} passes jobs.error through verbatim, so the
    full frozen Error shape must be a STORAGE invariant, not writer
    discipline — a partial object is refused at the write. And because 0014
    adds this CHECK only after backfilling, the constraint's existence on a
    migrated database proves no legacy partial row survived the upgrade."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)
            job = await create_job(conn, project, "build")
            with pytest.raises(IntegrityError):
                await set_progress(
                    conn,
                    job.id,
                    status="failed",
                    error={"code": "INTERNAL", "message": "no request_id"},
                )
            await trans.rollback()
    finally:
        await engine.dispose()


# ── find_unenqueued_jobs (BA2e queued-sweep) ─────────────────────────────────


async def test_find_unenqueued_jobs_matches_only_lost_queued_rows(migrated: None) -> None:
    """WHY: the sweep re-dispatches, so a false match risks a duplicate build.
    Only a job that should long since have been dispatched and shows no trace
    of one (still `queued`, never leased, older than the grace) may match: a
    young row may still be mid-trigger; a leased row is the expired-lease
    sweep's; a running row was definitely dispatched (arq owns its retry); a
    terminal row is done."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)

            fresh = await create_job(conn, project, "build")
            lost = await create_job(conn, project, "build")
            leased = await create_job(conn, project, "build")
            running = await create_job(conn, project, "build")
            done = await create_job(conn, project, "build")
            await conn.execute(
                jobs.update()
                .where(jobs.c.id.in_([lost.id, leased.id, running.id, done.id]))
                .values(created_at=sa.func.now() - sa.text("interval '10 minutes'"))
            )
            await acquire_lease(conn, leased.id, "w1", 60.0)
            await set_progress(conn, running.id, status="running")
            await set_progress(conn, done.id, status="done")

            found = {j for j, *_ in await find_unenqueued_jobs(conn, 120.0)}
            mine = {fresh.id, lost.id, leased.id, running.id, done.id}
            assert found & mine == {lost.id}
            # each hit carries (project, kind, build_id) — the reaper dispatches by
            # kind (build/ingest → BUILD_TASK, eval → EVAL_TASK) and a build has no
            # build_id on the row
            assert (lost.id, project, "build", None) in await find_unenqueued_jobs(conn, 120.0)
            # the grace is respected: nothing 10 minutes old matches an hour-long grace
            assert {j for j, *_ in await find_unenqueued_jobs(conn, 3600.0)} & mine == set()
            await trans.rollback()
    finally:
        await engine.dispose()
