"""Why: the execution lease is a concurrency-correctness property that only holds
against real Postgres semantics — an atomic conditional UPDATE picking one winner,
DB-clock (not caller-clock) expiry, and owner-guarded renew/release. These tests
drive the primitives and the ``run_build_leased`` wrapper on live Postgres with
FAKE stages (no Qdrant/Neo4j/LLM), pinning:

* the primitives: acquire is exclusive until release; renew/release only by the
  owner; an expired lease is reclaimable.
* the wrapper (the headline): two dispatches of one job — one paused mid-stage
  holding the lease — execute the build exactly ONCE, the peer no-ops; and a
  crashed holder's expired lease never blocks the next dispatch.

They COMMIT (the orchestrator opens its own per-stage connections), so each test
sweeps its project's artifacts in a finally.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.lease import run_build_leased
from core.builds.orchestrator import _STAGE_ORDER, StageResult, Stages
from core.config import get_settings
from core.registry import create_job, create_project, get_job
from core.registry.jobs import (
    acquire_lease,
    capture_config_snapshot,
    release_lease,
    renew_lease,
)
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


def _noop_stage() -> _StageFn:
    async def stage(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        return StageResult()

    return stage


def _noop_stages(**overrides: _StageFn) -> Stages:
    return Stages(**{n: overrides.get(n) or _noop_stage() for n in _STAGE_ORDER})


async def _make_job(engine: AsyncEngine, project: str) -> uuid.UUID:
    async with engine.connect() as conn, conn.begin():
        await create_project(conn, name=project)
        job = await create_job(conn, project, "build")
    return job.id


async def _lease_owner(engine: AsyncEngine, job_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        return cast(
            "str | None",
            (
                await conn.execute(
                    sa.select(tables.jobs.c.lease_owner).where(tables.jobs.c.id == job_id)
                )
            ).scalar_one(),
        )


async def _job_row(engine: AsyncEngine, job_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return await get_job(conn, job_id)


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


# ── primitives ──────────────────────────────────────────────────────────────


async def test_acquire_is_exclusive_until_release(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        async with engine.begin() as conn:
            assert await acquire_lease(conn, job_id, "A", 60.0) is True
        async with engine.begin() as conn:
            assert await acquire_lease(conn, job_id, "B", 60.0) is False  # A holds a live lease
        async with engine.begin() as conn:
            await release_lease(conn, job_id, "A")
        async with engine.begin() as conn:
            assert await acquire_lease(conn, job_id, "B", 60.0) is True  # now free
    finally:
        await _cleanup(engine, project)


async def test_concurrent_acquire_picks_exactly_one_winner(migrated: None) -> None:
    # the class-10 core: two dispatches racing a free lease at the same instant.
    # The atomic conditional UPDATE (guard in the WHERE, not a prior read) must
    # resolve to exactly one winner — never both, never neither.
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)

        async def _try(owner: str) -> bool:
            async with engine.begin() as conn:
                return await acquire_lease(conn, job_id, owner, 60.0)

        results = await asyncio.gather(_try("A"), _try("B"))
        assert sorted(results) == [False, True]  # one won the row-lock race, one lost
    finally:
        await _cleanup(engine, project)


async def test_acquire_rejects_an_empty_owner(migrated: None) -> None:
    # the owner-guard is load-bearing: an empty owner id would let any two
    # empty-owner workers renew/release each other's lease, so the DB rejects it
    # (jobs_lease_owner_nonempty) rather than store a lease no one uniquely owns.
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await acquire_lease(conn, job_id, "", 60.0)
    finally:
        await _cleanup(engine, project)


async def test_renew_and_release_are_owner_guarded(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        async with engine.begin() as conn:
            await acquire_lease(conn, job_id, "A", 60.0)
        async with engine.begin() as conn:
            assert await renew_lease(conn, job_id, "A", 120.0) is True  # owner renews
        async with engine.begin() as conn:
            assert await renew_lease(conn, job_id, "B", 60.0) is False  # non-owner cannot
        async with engine.begin() as conn:
            await release_lease(conn, job_id, "B")  # non-owner release: no-op
        async with engine.begin() as conn:
            assert await acquire_lease(conn, job_id, "C", 60.0) is False  # A still holds it
    finally:
        await _cleanup(engine, project)


async def test_expired_lease_is_reclaimable(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        async with engine.begin() as conn:
            await acquire_lease(conn, job_id, "A", 60.0)
            # simulate a crashed holder whose heartbeat lapsed: push expiry into
            # the past on the DB clock.
            await conn.execute(
                tables.jobs.update()
                .where(tables.jobs.c.id == job_id)
                .values(lease_expires_at=sa.text("now() - interval '1 minute'"))
            )
        async with engine.begin() as conn:
            assert await acquire_lease(conn, job_id, "B", 60.0) is True  # reclaimed
        assert await _lease_owner(engine, job_id) == "B"
    finally:
        await _cleanup(engine, project)


# ── config pin ──────────────────────────────────────────────────────────────


async def _stored_snapshot(engine: AsyncEngine, job_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(tables.jobs.c.config_snapshot).where(tables.jobs.c.id == job_id)
            )
        ).scalar_one()


async def _null_snapshot(engine: AsyncEngine, job_id: uuid.UUID) -> None:
    # simulate a job with no pinned config (a legacy row predating create_job's
    # capture) to exercise the worker's defensive read-or-set fallback. sa.null()
    # writes SQL NULL — the real absent state a legacy column reads as — not a JSONB
    # 'null' literal (which COALESCE would treat as present).
    async with engine.begin() as conn:
        await conn.execute(
            tables.jobs.update().where(tables.jobs.c.id == job_id).values(config_snapshot=sa.null())
        )


async def test_create_job_pins_config_at_creation_and_survives_drift(migrated: None) -> None:
    # the config-drift guard (BA2d-2): a build must run the config the user
    # SUBMITTED, not whatever the project holds when the worker first dispatches.
    # create_job pins the project config at creation; a later PATCH /projects
    # (drift) doesn't change the pin, and the worker's capture reads the pin back.
    engine = _engine()
    project = _proj()
    submitted = {"chunking": {"max_chars": 100}}
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project, config=submitted)
            job = await create_job(conn, project, "build")
        # create_job pinned the config the build was submitted with…
        assert await _stored_snapshot(engine, job.id) == submitted
        # …and the worker reuses that pin even though live config has since drifted.
        drifted = {"chunking": {"max_chars": 999}}
        async with engine.begin() as conn:
            effective = await capture_config_snapshot(conn, job.id, drifted)
        assert effective == submitted
        assert await _stored_snapshot(engine, job.id) == submitted
    finally:
        await _cleanup(engine, project)


async def test_capture_defensively_pins_when_snapshot_absent(migrated: None) -> None:
    # a job that somehow lacks a snapshot must still be pinned: the first capture
    # writes C1, a later capture with a drifted C2 returns C1 — the atomic COALESCE
    # keeps the first-written config so a resume never picks up drifted params.
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        await _null_snapshot(engine, job_id)
        c1 = {"chunking": {"max_chars": 100}}
        c2 = {"chunking": {"max_chars": 999}}
        async with engine.begin() as conn:
            first = await capture_config_snapshot(conn, job_id, c1)
        async with engine.begin() as conn:
            second = await capture_config_snapshot(conn, job_id, c2)  # drifted live config
        assert first == c1
        assert second == c1  # re-dispatch reuses the pinned config, ignores c2
        assert await _stored_snapshot(engine, job_id) == c1
    finally:
        await _cleanup(engine, project)


async def test_concurrent_capture_converges_on_one_config(migrated: None) -> None:
    # two dispatches racing the pin of a snapshot-less job must converge on a single
    # stored config — the atomic COALESCE + row lock, the same single-winner property
    # as the lease's acquire. Never a mix, never one dispatch building from C_a while
    # the row stores C_b.
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        await _null_snapshot(engine, job_id)
        ca = {"chunking": {"max_chars": 1}}
        cb = {"chunking": {"max_chars": 2}}

        async def _cap(cfg: dict[str, Any]) -> Any:
            async with engine.begin() as conn:
                return await capture_config_snapshot(conn, job_id, cfg)

        a, b = await asyncio.gather(_cap(ca), _cap(cb))
        assert a == b  # both dispatches see the same pinned config…
        assert a in (ca, cb)  # …one of the two racing configs, atomically chosen
        assert await _stored_snapshot(engine, job_id) == a
    finally:
        await _cleanup(engine, project)


# ── wrapper ─────────────────────────────────────────────────────────────────


async def test_concurrent_dispatch_executes_the_build_exactly_once(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        job_id = await _make_job(engine, project)

        async def _pausing_ingest(
            conn: AsyncConnection, project: str, build_id: uuid.UUID
        ) -> StageResult:
            entered.set()  # A has the lease and is now mid-build
            await release.wait()
            return StageResult()

        # A acquires the lease and parks in ingest; B then dispatches while A holds it.
        task_a = asyncio.create_task(
            run_build_leased(
                engine,
                project,
                job_id,
                _noop_stages(ingest=_pausing_ingest),
                owner="A",
                step_failure_ratio=0.0,
            )
        )
        await entered.wait()

        result_b = await run_build_leased(
            engine, project, job_id, _noop_stages(), owner="B", step_failure_ratio=0.0
        )
        assert result_b is None  # B saw a live lease → deliberate no-op

        release.set()
        result_a = await task_a
        assert result_a is not None and result_a.status == "ready"

        # exactly one build was created and run; the job finished and the lease
        # was released.
        async with engine.connect() as conn:
            builds = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.builds)
                    .where(tables.builds.c.project == project)
                )
            ).scalar_one()
        assert builds == 1
        job = await _job_row(engine, job_id)
        assert job.status == "done"
        assert await _lease_owner(engine, job_id) is None
    finally:
        release.set()  # never leave task_a parked if an assert failed
        await _cleanup(engine, project)


async def test_a_crashed_holders_expired_lease_does_not_block_a_new_dispatch(
    migrated: None,
) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _make_job(engine, project)
        # a worker crashed mid-build holding the lease; its expiry has since lapsed.
        async with engine.begin() as conn:
            await acquire_lease(conn, job_id, "crashed", 60.0)
            await conn.execute(
                tables.jobs.update()
                .where(tables.jobs.c.id == job_id)
                .values(lease_expires_at=sa.text("now() - interval '1 minute'"))
            )
        result = await run_build_leased(
            engine, project, job_id, _noop_stages(), owner="fresh", step_failure_ratio=0.0
        )
        assert result is not None and result.status == "ready"  # reclaimed and ran
        assert await _lease_owner(engine, job_id) is None  # released on completion
    finally:
        await _cleanup(engine, project)
