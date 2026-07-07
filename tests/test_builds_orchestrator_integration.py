"""Why: the orchestrator is the missing first arrow of the §5 pipeline — it
turns a job into a `building` build, runs the six stages in order, and lands the
build at `ready`/`failed`. Its correctness is entirely a Postgres-bookkeeping
property (build state, the §18 run/step rows, the job's live fields), decoupled
from what any real stage does — so these drive `run_build` with FAKE stages
(canned StageResults, or one that raises / requests cancel) against live
Postgres, with zero Qdrant/Neo4j/LLM. They pin: the happy path building→ready
with all six §5 steps; a stage crash failing the build (not the worker); the §22
failed-ratio abort; cooperative cancel recording only the steps that ran;
resume of a still-building build; and the resumability/ownership guards.

These COMMIT (the orchestrator opens its own connections per stage), so each
test cleans up its project's artifacts in a finally block.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.creation import create_build
from core.builds.orchestrator import (
    _STAGE_ORDER,
    BuildNotResumableError,
    StageResult,
    Stages,
    run_build,
)
from core.config import get_settings
from core.observability.spec import ItemOutcome
from core.registry import create_job, create_project, get_job, request_cancel
from core.registry import jobs as jobs_module
from core.registry.jobs import JobNotFoundError
from core.stores import tables

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_StageFn = Callable[[AsyncConnection, str, uuid.UUID], Awaitable[StageResult]]
_Hook = Callable[[AsyncConnection, str, uuid.UUID], Awaitable[None]]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


def _recording_stage(
    name: str,
    calls: list[str],
    *,
    outcomes: tuple[ItemOutcome, ...] = (),
    exc: Exception | None = None,
    hook: _Hook | None = None,
) -> _StageFn:
    """A fake stage: records that it ran, optionally runs a side-effect hook
    (e.g. request a cancel), optionally raises, else returns canned outcomes."""

    async def stage(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        calls.append(name)
        if hook is not None:
            await hook(conn, project, build_id)
        if exc is not None:
            raise exc
        return StageResult(outcomes=outcomes)

    return stage


def _stages(calls: list[str], **overrides: _StageFn) -> Stages:
    """The six §5 stages, each a plain recording no-op unless overridden."""
    return Stages(**{n: overrides.get(n) or _recording_stage(n, calls) for n in _STAGE_ORDER})


async def _new_job(engine: AsyncEngine, project: str) -> uuid.UUID:
    async with engine.connect() as conn, conn.begin():
        await create_project(conn, name=project)
        job = await create_job(conn, project, "build")
    return job.id


async def _cleanup(engine: AsyncEngine, project: str) -> None:
    """Remove everything a run committed for this project (FK-safe order:
    observability rows, jobs, builds, then the project — builds→projects is
    RESTRICT so the project goes last)."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            tables.pipeline_step_items.delete().where(
                tables.pipeline_step_items.c.step_id.in_(
                    sa.select(tables.pipeline_steps.c.id)
                    .join(
                        tables.pipeline_runs,
                        tables.pipeline_steps.c.run_id == tables.pipeline_runs.c.id,
                    )
                    .where(tables.pipeline_runs.c.project == project)
                )
            )
        )
        await conn.execute(
            tables.pipeline_steps.delete().where(
                tables.pipeline_steps.c.run_id.in_(
                    sa.select(tables.pipeline_runs.c.id).where(
                        tables.pipeline_runs.c.project == project
                    )
                )
            )
        )
        await conn.execute(
            tables.pipeline_runs.delete().where(tables.pipeline_runs.c.project == project)
        )
        await conn.execute(tables.jobs.delete().where(tables.jobs.c.project == project))
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == project))
        await conn.execute(tables.projects.delete().where(tables.projects.c.name == project))


async def _build_row(engine: AsyncEngine, build_id: uuid.UUID) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(
                    tables.builds.c.status, tables.builds.c.metrics, tables.builds.c.finished_at
                ).where(tables.builds.c.id == build_id)
            )
        ).one()


async def _step_names(engine: AsyncEngine, run_id: uuid.UUID) -> set[str]:
    async with engine.connect() as conn:
        return set(
            (
                await conn.execute(
                    sa.select(tables.pipeline_steps.c.step_name).where(
                        tables.pipeline_steps.c.run_id == run_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def _run_status(engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        status = (
            await conn.execute(
                sa.select(tables.pipeline_runs.c.status).where(tables.pipeline_runs.c.id == run_id)
            )
        ).scalar_one()
    return str(status)


def test_stage_order_is_the_frozen_design_5_sequence() -> None:
    """§5: ingest → clean → graph → resolve → index → summarize. Pinned so a
    reordering (which would corrupt every build) fails loudly here."""
    assert _STAGE_ORDER == ("ingest", "clean", "graph", "resolve", "index", "summarize")


async def test_happy_path_runs_all_six_stages_and_reaches_ready(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []

        outcome = await run_build(engine, project, job_id, _stages(calls))

        assert outcome.status == "ready"
        assert not outcome.cancelled
        assert outcome.error is None
        assert calls == list(_STAGE_ORDER)  # every stage ran, in §5 order

        build = await _build_row(engine, outcome.build_id)
        assert build.status == "ready"
        assert build.metrics["cancelled"] is False
        assert set(build.metrics["steps"]) == set(_STAGE_ORDER)
        assert build.finished_at is not None

        assert await _step_names(engine, outcome.run_id) == set(_STAGE_ORDER)
        assert await _run_status(engine, outcome.run_id) == "done"

        async with engine.connect() as conn:
            job = await get_job(conn, job_id)
        assert job is not None
        assert job.status == "done"
        assert job.progress == 1.0
        assert job.build_id == outcome.build_id
        assert job.finished_at is not None
        assert job.error is None
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_a_stage_crash_fails_the_build_not_the_worker(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []
        boom = _recording_stage("graph", calls, exc=RuntimeError("extractor exploded"))

        outcome = await run_build(engine, project, job_id, _stages(calls, graph=boom))

        assert outcome.status == "failed"
        assert not outcome.cancelled
        assert outcome.error is not None and "graph:" in outcome.error
        # stopped at graph → only ingest+clean recorded, graph onward absent
        assert calls == ["ingest", "clean", "graph"]
        assert await _step_names(engine, outcome.run_id) == {"ingest", "clean"}
        assert await _run_status(engine, outcome.run_id) == "failed"

        build = await _build_row(engine, outcome.build_id)
        assert build.status == "failed"

        async with engine.connect() as conn:
            job = await get_job(conn, job_id)
        assert job is not None and job.status == "failed"
        # the full §15 Error shape the jobs.error column documents (code+message+
        # details), not a bare message — BA2e passes it straight through
        assert job.error == {"code": "INTERNAL", "message": outcome.error, "details": None}
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_a_step_over_the_failure_ratio_aborts_the_build(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []
        # 2 of 3 documents failed → ratio 0.67 > threshold 0.5 → abort at clean
        flaky = _recording_stage(
            "clean",
            calls,
            outcomes=(
                ItemOutcome("document", "a", "failed"),
                ItemOutcome("document", "b", "failed"),
                ItemOutcome("document", "c", "skipped"),
            ),
        )

        outcome = await run_build(
            engine, project, job_id, _stages(calls, clean=flaky), step_failure_ratio=0.5
        )

        assert outcome.status == "failed"
        assert outcome.error is not None and "§22" in outcome.error
        assert calls == ["ingest", "clean"]  # aborted before graph
        assert await _run_status(engine, outcome.run_id) == "failed"
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_a_step_under_the_failure_ratio_does_not_abort(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []
        # 1 of 3 failed → ratio 0.33 < 0.5 → the build continues to ready
        tolerated = _recording_stage(
            "graph",
            calls,
            outcomes=(
                ItemOutcome("document", "a", "failed"),
                ItemOutcome("document", "b", "skipped"),
                ItemOutcome("document", "c", "skipped"),
            ),
        )

        outcome = await run_build(
            engine, project, job_id, _stages(calls, graph=tolerated), step_failure_ratio=0.5
        )

        assert outcome.status == "ready"
        assert calls == list(_STAGE_ORDER)
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_cancel_between_stages_records_only_the_steps_that_ran(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []

        async def _cancel(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> None:
            # flip cancel_requested inside ingest's own txn; committed with the
            # stage, so the checkpoint before clean sees it
            await request_cancel(conn, job_id)

        ingest = _recording_stage("ingest", calls, hook=_cancel)

        outcome = await run_build(engine, project, job_id, _stages(calls, ingest=ingest))

        assert outcome.cancelled
        assert outcome.status == "failed"  # cancelled reuses builds.status='failed'
        assert outcome.error is None
        assert calls == ["ingest"]  # clean onward never ran
        assert await _step_names(engine, outcome.run_id) == {"ingest"}
        assert await _run_status(engine, outcome.run_id) == "cancelled"

        build = await _build_row(engine, outcome.build_id)
        assert build.status == "failed"
        assert build.metrics["cancelled"] is True  # the distinction Health reads

        async with engine.connect() as conn:
            job = await get_job(conn, job_id)
        assert job is not None and job.status == "cancelled"
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_cancel_before_the_first_stage_records_no_steps(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        async with engine.connect() as conn, conn.begin():
            await request_cancel(conn, job_id)  # cancel while still queued
        calls: list[str] = []

        outcome = await run_build(engine, project, job_id, _stages(calls))

        assert outcome.cancelled
        assert calls == []  # not even ingest ran
        assert await _step_names(engine, outcome.run_id) == set()
        assert await _run_status(engine, outcome.run_id) == "cancelled"
        # a build row was still created and marked failed/cancelled
        build = await _build_row(engine, outcome.build_id)
        assert build.status == "failed"
        assert build.metrics["cancelled"] is True
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_resume_reuses_the_building_build_it_is_given(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        # a build left 'building' (e.g. the worker died before the terminal flip)
        async with engine.connect() as conn, conn.begin():
            existing = await create_build(conn, project)
        calls: list[str] = []

        outcome = await run_build(engine, project, job_id, _stages(calls), build_id=existing)

        assert outcome.build_id == existing  # resumed, not a fresh build
        assert outcome.status == "ready"
        # exactly one build for this project — resume didn't mint a second
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.builds)
                    .where(tables.builds.c.project == project)
                )
            ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_a_finished_build_is_not_resumable(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        async with engine.connect() as conn, conn.begin():
            done = await create_build(conn, project)
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == done).values(status="ready")
            )
        calls: list[str] = []

        with pytest.raises(BuildNotResumableError):
            await run_build(engine, project, job_id, _stages(calls), build_id=done)
        assert calls == []  # never entered the stage loop
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_run_build_guards_job_existence_and_ownership(migrated: None) -> None:
    engine = _engine()
    project = _proj()
    other = _proj()
    try:
        job_id = await _new_job(engine, project)
        calls: list[str] = []

        # unknown job → JobNotFoundError
        with pytest.raises(JobNotFoundError):
            await run_build(engine, project, uuid.uuid4(), _stages(calls))

        # right job, wrong project → refuse loudly, don't misattribute
        with pytest.raises(ValueError, match="belongs to project"):
            await run_build(engine, other, job_id, _stages(calls))

        assert calls == []
    finally:
        await _cleanup(engine, project)
        await engine.dispose()


async def test_build_creation_and_job_attach_are_atomic(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between minting the build and attaching it to the job must not
    orphan a 'building' build (unresumable — the job still points at NULL — and
    RESTRICT blocks deleting it with the project). They share one transaction:
    if the attach fails, the build creation rolls back and NO build persists. A
    two-transaction version would leak the orphan, which this test catches."""
    engine = _engine()
    project = _proj()
    try:
        job_id = await _new_job(engine, project)
        real_set_progress = jobs_module.set_progress

        async def _fail_on_attach(conn: AsyncConnection, jid: uuid.UUID, **kw: Any) -> Any:
            if "build_id" in kw:  # the create→attach step
                raise RuntimeError("attach exploded")
            return await real_set_progress(conn, jid, **kw)

        monkeypatch.setattr(jobs_module, "set_progress", _fail_on_attach)
        calls: list[str] = []

        with pytest.raises(RuntimeError, match="attach exploded"):
            await run_build(engine, project, job_id, _stages(calls))

        # the failed attach rolled the build creation back — no orphan build
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.builds)
                    .where(tables.builds.c.project == project)
                )
            ).scalar_one()
        assert count == 0
        assert calls == []  # never reached the stage loop
    finally:
        await _cleanup(engine, project)
        await engine.dispose()
