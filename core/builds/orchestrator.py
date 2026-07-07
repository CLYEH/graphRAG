"""Build pipeline orchestrator (BA2c) — chains the six §5 stages into a build.

This is the control flow only: it creates (or resumes) a ``building`` build,
runs the §5 stages in order (ingest → clean → graph → resolve → index →
summarize), records each as a §18 pipeline step, honours cooperative
cancellation (§ jobs) between stages and the §22 per-step failure-ratio abort,
then flips the build to ``ready`` (success) or ``failed`` (a stage error, a
threshold breach, or a cancel) and records the run once (§18).

**Stops at ``ready``.** Activation (``ready → active``) is the §14/§20
eval-gated single-transaction step owned by ``core.builds.lifecycle.activate``
(C9) — not this function.

**The stage seam.** Every stage module re-reads its own inputs from Postgres
(the SoR is the only hand-off between stages — the convergent-idempotency
design), so a stage adapter is a uniform ``(conn, project, build_id) ->
StageResult`` callable. Keeping every store/LLM/config dependency inside those
closures (built by BA2c-2's ``default_stages``) is what makes this control flow
hermetically testable with fakes — no Qdrant/Neo4j/LLM needed to prove the
sequencing, step recording, cancellation, and failure handling.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.builds.creation import create_build
from core.observability.recorder import StepReport, record_run
from core.observability.spec import ItemOutcome
from core.registry import jobs
from core.stores import tables

#: §5 pipeline order — the six stages run in exactly this sequence. Named
#: explicitly so the §5 contract is pinned independently of Stages' field order.
_STAGE_ORDER = ("ingest", "clean", "graph", "resolve", "index", "summarize")

#: the run kind recorded in pipeline_runs for a full build (§5: 每次 build 開
#: pipeline_runs). Not build-unbound, so record_run requires the build id.
BUILD_RUN_KIND = "build"


@dataclass(frozen=True)
class StageResult:
    """One stage's result, recorded by the orchestrator as a §18 pipeline step.

    ``outcomes`` are the per-item results (pipeline_step_items rows keyed by a
    stable §18 item_ref); an empty tuple is legal — resolve has no natural
    per-item retry unit, only aggregate counts. ``detail`` is the stage's own
    report, folded into builds.metrics for humans/Health, never interpreted by
    the orchestrator's control flow.
    """

    outcomes: tuple[ItemOutcome, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)


#: A stage adapter: given a connection, the project, and the building build id,
#: run the stage (re-reading its inputs from Postgres) and return its outcomes.
StageFn = Callable[[AsyncConnection, str, uuid.UUID], Awaitable[StageResult]]


@dataclass(frozen=True)
class Stages:
    """The six §5 stage adapters, in pipeline order — the orchestrator's single
    injection point. Production (BA2c-2) closes each over its real deps; tests
    inject fakes with zero store contact."""

    ingest: StageFn
    clean: StageFn
    graph: StageFn
    resolve: StageFn
    index: StageFn
    summarize: StageFn


@dataclass(frozen=True)
class BuildOutcome:
    """What ``run_build`` resolved to. ``status`` is the terminal builds.status
    (``ready`` | ``failed``); ``cancelled`` distinguishes a cooperative cancel
    from a real failure — both land builds.status=``failed``, but jobs and
    pipeline_runs carry ``cancelled`` and builds.metrics.cancelled is true."""

    build_id: uuid.UUID
    run_id: uuid.UUID
    status: str
    cancelled: bool
    error: str | None


class BuildNotResumableError(LookupError):
    """``run_build`` was given a build_id that is not a ``building`` build of
    this project (already ready/failed/active/archived, another project's, or
    unknown). A finished build cannot be resumed in place — start a fresh
    build. (Resume only applies to a build still mid-flight, e.g. the worker
    died before the terminal transition.)"""

    def __init__(self, project: str, build_id: uuid.UUID, status: str | None) -> None:
        super().__init__(
            f"build {build_id} is not resumable in project {project} "
            f"(status={status or 'missing'}; needs 'building')"
        )
        self.project = project
        self.build_id = build_id
        self.status = status


def _default_threshold() -> float:
    from core.config import get_settings

    return get_settings().pipeline_step_failure_ratio


async def _build_status(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> str | None:
    """The build's status if it exists in this project, else None."""
    return (
        await conn.execute(
            sa.select(tables.builds.c.status).where(
                tables.builds.c.id == build_id, tables.builds.c.project == project
            )
        )
    ).scalar_one_or_none()


async def run_build(
    engine: AsyncEngine,
    project: str,
    job_id: uuid.UUID,
    stages: Stages,
    *,
    build_id: uuid.UUID | None = None,
    config_hash: str | None = None,
    source_hash: str | None = None,
    step_failure_ratio: float | None = None,
) -> BuildOutcome:
    """Run the §5 build pipeline for one job, end to end.

    ``build_id=None`` (and a job with no build yet) creates a fresh build;
    otherwise the named build is resumed (must still be ``building``, else
    ``BuildNotResumableError``). Each stage runs in its own committed
    transaction — the convergent-idempotency seam, so a partial build resumes
    by re-running the stages (each skips its already-done work). Raises
    ``jobs.JobNotFoundError`` for an unknown job. ``step_failure_ratio``
    overrides the §22 settings default (tests pass it explicitly).
    """
    threshold = step_failure_ratio if step_failure_ratio is not None else _default_threshold()

    # 1. load the job; guard project ownership (a misrouted job is loud, not silent)
    async with engine.connect() as conn:
        job = await jobs.get_job(conn, job_id)
    if job is None:
        raise jobs.JobNotFoundError(job_id)
    if job.project != project:
        raise ValueError(f"job {job_id} belongs to project {job.project!r}, not {project!r}")

    # 2. resolve the build: fresh (create + attach) or resume (validate building)
    resume_id = build_id if build_id is not None else job.build_id
    if resume_id is None:
        # create the build AND attach it to the job in ONE transaction — else a
        # crash between the two commits leaves the job at build_id=NULL pointing
        # at nothing while an orphaned 'building' build persists (unresumable,
        # and RESTRICT blocks deleting it with the project). Atomic → a retry
        # either finds the attached build (resume) or a clean slate (create).
        async with engine.connect() as conn, conn.begin():
            build_id = await create_build(
                conn, project, config_hash=config_hash, source_hash=source_hash
            )
            await jobs.set_progress(conn, job_id, build_id=build_id)
    else:
        build_id = resume_id
        async with engine.connect() as conn:
            status = await _build_status(conn, project, build_id)
        if status != "building":
            raise BuildNotResumableError(project, build_id, status)
        if job.build_id != build_id:
            async with engine.connect() as conn, conn.begin():
                await jobs.set_progress(conn, job_id, build_id=build_id)

    # 3. mark running
    async with engine.connect() as conn, conn.begin():
        await jobs.set_progress(conn, job_id, status="running", progress=0.0)

    # 4. run the six stages in §5 order
    step_reports: list[StepReport] = []
    metrics_steps: dict[str, dict[str, int]] = {}
    error: str | None = None
    cancelled = False
    for i, name in enumerate(_STAGE_ORDER):
        # a. cooperative cancel checkpoint (before the stage runs — covers a
        #    cancel that lands the instant the job is picked up)
        async with engine.connect() as conn:
            if await jobs.is_cancel_requested(conn, job_id):
                cancelled = True
                break
        # b. run the stage in its own committed transaction; ANY failure fails
        #    the build (§22 structural path — store outages, bugs. Per-item
        #    LLM/parse failures never reach here: each stage records those
        #    internally as failed outcomes and returns normally).
        stage_fn = getattr(stages, name)
        try:
            async with engine.connect() as conn, conn.begin():
                result = await stage_fn(conn, project, build_id)
        except Exception as exc:  # noqa: BLE001 — the build boundary: any stage crash → failed build, never a dead worker
            error = f"{name}: {exc}"
            break
        # c. record the step + fold its counts into metrics
        step_reports.append(StepReport(name, result.outcomes))
        failed = sum(1 for o in result.outcomes if o.status == "failed")
        skipped = sum(1 for o in result.outcomes if o.status == "skipped")
        metrics_steps[name] = {"total": len(result.outcomes), "failed": failed, "skipped": skipped}
        # d. §22 abort: this step's failed-item ratio exceeds the threshold
        if result.outcomes and failed / len(result.outcomes) > threshold:
            error = f"{name}: {failed}/{len(result.outcomes)} items failed (> {threshold}, §22)"
            break
        # e. progress
        async with engine.connect() as conn, conn.begin():
            await jobs.set_progress(conn, job_id, step=name, progress=(i + 1) / len(_STAGE_ORDER))

    # 5. terminal builds.status + a small metrics summary (cancelled reuses
    #    'failed' — no 'cancelled' value in the frozen BUILD_STATUSES enum;
    #    metrics.cancelled keeps the distinction for Health without a schema change)
    build_status = "ready" if (error is None and not cancelled) else "failed"
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            tables.builds.update()
            .where(tables.builds.c.id == build_id)
            .values(
                status=build_status,
                finished_at=sa.func.now(),
                metrics=sa.cast({"steps": metrics_steps, "cancelled": cancelled}, postgresql.JSONB),
            )
        )

    # 6. record the run once (loaned-clean connection — record_run opens its own txn)
    async with engine.connect() as conn:
        run_id = await record_run(
            conn,
            project,
            build_id,
            BUILD_RUN_KIND,
            step_reports,
            error=error,
            cancelled=cancelled,
        )

    # 7. terminal job state. finished_at uses the DB clock (single clock source
    #    — jobs.created_at/builds.* are all now()); error is the full §15 Error
    #    shape the jobs.error column documents ({code, message, details}) — the
    #    request_id is filled by BA2e's GET /jobs serializer.
    job_status = "cancelled" if cancelled else ("failed" if error is not None else "done")
    progress_fields: dict[str, Any] = {"status": job_status, "finished_at": sa.func.now()}
    if error is not None:
        progress_fields["error"] = {"code": "INTERNAL", "message": error, "details": None}
    async with engine.connect() as conn, conn.begin():
        await jobs.set_progress(conn, job_id, **progress_fields)

    return BuildOutcome(
        build_id=build_id,
        run_id=run_id,
        status=build_status,
        cancelled=cancelled,
        error=error,
    )
