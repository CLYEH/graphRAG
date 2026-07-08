"""BA2d-2 arq build worker — Redis queue → the §5 build pipeline.

DESIGN §11 runs a separate ``worker`` container that consumes the jobs queue and
executes builds; the API (BA2e) only enqueues. This wires arq + Redis onto core:

* ``on_startup`` builds ONE long-lived dep bundle (engine + Qdrant/Neo4j/LLM/
  embedder — the ``ProjectContext`` shape, pooled and reused across jobs) plus a
  unique owner id for this worker process.
* ``run_build_task`` reuses the build's pinned config (``create_job`` snapshots it
  at job creation; reused on every dispatch so neither a queue-delay config edit
  nor a re-dispatch can drift a resume), builds the six §5 stages off the bundle,
  and executes under the BA2d-1 execution lease (``run_build_leased``) so a
  duplicate dispatch of the same job is a no-op rather than a second concurrent
  execution.
* ``enqueue_build`` (BA2e's trigger calls it after ``create_job``) enqueues with
  ``_job_id=str(job_id)`` for arq's own dispatch dedup.
* ``reap_stuck_builds`` (a cron, BA2d-3) re-enqueues builds whose worker crashed —
  found by an expired execution lease — so crash recovery is fast (~1 min) and
  decoupled from arq's generous job_timeout; the DB lease keeps it a single
  executor. This makes the BA2d-1 heartbeat-lease the sole build-liveness authority.

Two dedup layers, by design: arq's ``_job_id`` refuses to *enqueue* a duplicate
while one is queued/running (the cheap first line); the DB heartbeat-lease is the
crash-safe backstop for the *execution* itself (a worker that dies mid-build has
its lease expire so the job is reclaimable). The worker never trusts arq's own
job status — the ``jobs`` row is the SoR (§27.7).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from arq import cron
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import BuildConfigError, load_build_config
from core.builds.lease import run_build_leased
from core.builds.orchestrator import BuildNotResumableError
from core.builds.stages import default_stages
from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.registry import (
    capture_config_snapshot,
    find_reapable_jobs,
    get_project,
    set_progress,
)
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

logger = logging.getLogger(__name__)

#: arq task name — enqueue and the WorkerSettings registration must agree, so the
#: string is defined once (arq registers a plain coroutine under its __name__).
BUILD_TASK = "run_build_task"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def enqueue_build(redis: ArqRedis, project: str, job_id: uuid.UUID) -> None:
    """Enqueue a build for the worker. ``_job_id=str(job_id)`` gives arq's own
    dispatch dedup — it refuses to enqueue a duplicate while the job is
    queued/running — the cheap first line of defense; the DB execution lease is
    the crash-safe backstop. BA2e's trigger endpoint calls this after create_job.
    """
    await redis.enqueue_job(BUILD_TASK, project, str(job_id), _job_id=str(job_id))


async def reenqueue_build(redis: ArqRedis, project: str, job_id: uuid.UUID) -> None:
    """Re-dispatch a crashed build (BA2d-3 reaper). Unlike ``enqueue_build`` this
    passes NO ``_job_id``: the crashed job's original arq in-progress key lingers
    for job_timeout+10s (the generous 24h backstop), so a same-id enqueue would be
    refused — a fresh arq id re-dispatches it now. The DB execution lease still
    guarantees a single executor (a redundant re-dispatch acquires nothing and
    no-ops), so an occasional duplicate enqueue between reaper ticks is harmless."""
    await redis.enqueue_job(BUILD_TASK, project, str(job_id))


async def reap_stuck_builds(ctx: dict[str, Any]) -> int:
    """BA2d-3 cron: re-enqueue builds whose worker crashed, decoupling crash
    recovery from arq's (now generous) job_timeout.

    A crashed/starved worker stops heartbeating, so its job's execution lease
    expires on the DB clock. This finds those (``find_reapable_jobs`` — expired
    held lease + non-terminal job) and re-enqueues each under a fresh arq id, so a
    fresh dispatch reclaims the now-free lease and resumes (~1-min recovery vs the
    24h backstop). An idle tick is a no-op; ``unique=True`` (see WorkerSettings)
    runs this on one worker per tick. Returns the count reaped."""
    engine: AsyncEngine = ctx["engine"]
    redis: ArqRedis = ctx["redis"]
    async with engine.connect() as conn:
        reapable = await find_reapable_jobs(conn)
    for job_id, project in reapable:
        await reenqueue_build(redis, project, job_id)
    if reapable:
        logger.info("reaped %d stuck build(s): %s", len(reapable), [str(j) for j, _ in reapable])
    return len(reapable)


async def run_build_task(ctx: dict[str, Any], project: str, job_id: str) -> str | None:
    """arq task: run one build under the execution lease.

    Reads the long-lived deps from ``ctx`` (built in ``on_startup``), pins+loads
    the project's config (see the preflight), builds the six §5 stages, and
    executes via ``run_build_leased`` with this worker's owner id. Returns the
    terminal build status, or ``None`` if a live peer already holds the lease (this
    dispatch was a deliberate no-op). A neo4j session is opened per job (the driver
    is shared); ``run_build`` opens its own per-stage Postgres transactions off the
    engine.
    """
    engine = ctx["engine"]
    build_job = uuid.UUID(job_id)
    # Preflight (project existence + config) happens BEFORE run_build enters the
    # orchestrator path that marks jobs.status. A deterministic failure here
    # (vanished project / malformed config) would otherwise leave the durable jobs
    # row queued forever — blocking project delete and misleading GET /jobs — so
    # record it on the row and don't retry (a retry can't fix it). Transient errors
    # (e.g. a DB blip) are NOT caught, so arq still retries those.
    #
    # The config is PINNED to the build: create_job snapshots proj.config onto the
    # job at creation, and capture_config_snapshot reads that pinned config back on
    # every dispatch (defensively pinning live config if a job somehow lacks one),
    # so neither a PATCH /projects during the queue delay nor a re-dispatch (an arq
    # retry, or the BA2d-3 reaper) can drift a resuming build's chunking/ontology
    # params. The defensive pin can write the jobs row, so this runs in a committing
    # begin().
    try:
        async with engine.begin() as conn:
            proj = await get_project(conn, project)
            if proj is None:
                raise LookupError(f"project {project!r} does not exist")
            raw_config = await capture_config_snapshot(conn, build_job, proj.config)
        config = load_build_config(raw_config)
    except (LookupError, BuildConfigError) as exc:
        await _fail_job(engine, build_job, exc)
        return "failed"
    async with ctx["neo4j"].session() as session:
        stages = default_stages(
            config,
            chat_model=ctx["llm"],
            embedder=ctx["embedder"],
            vector_client=ctx["qdrant"],
            graph_session=session,
        )
        try:
            outcome = await run_build_leased(engine, project, build_job, stages, owner=ctx["owner"])
        except BuildNotResumableError:
            # Benign recovery race: a re-dispatch (an arq retry, or the BA2d-3 reaper)
            # acquired the lease AFTER the original — starved, not dead — worker
            # terminalized the build and released it. run_build's FOR-UPDATE-locked
            # build-status check is the atomic recheck: it found the build already
            # terminal, so recovery wasn't needed. This dispatch is a no-op, not a
            # failure — don't manufacture a failed/retried arq job for a build that
            # already succeeded (the jobs row is already terminal, set by that worker).
            return None
        return outcome.status if outcome is not None else None


async def _fail_job(engine: AsyncEngine, job_id: uuid.UUID, exc: Exception) -> None:
    """Record a preflight failure on the durable jobs row (its own committed
    transaction), so a build that never reached the orchestrator still terminates
    the job instead of leaving it queued."""
    async with engine.begin() as conn:
        await set_progress(
            conn,
            job_id,
            status="failed",
            finished_at=sa.func.now(),
            error={"code": "INTERNAL", "message": str(exc), "details": None},
        )


async def on_startup(ctx: dict[str, Any]) -> None:
    """Build the long-lived dep bundle once (engines pooled/reused across jobs —
    the ProjectContext shape) plus a per-process owner id for the execution lease.
    """
    settings = get_settings()
    ctx["engine"] = create_async_engine(
        settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=NullPool,
    )
    ctx["qdrant"] = vector_client()
    ctx["neo4j"] = graph_driver()
    ctx["embedder"] = embedding_model()
    ctx["llm"] = chat_model()
    ctx["owner"] = f"worker-{uuid.uuid4().hex}"


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Release the long-lived engines at worker shutdown (mirror of on_startup)."""
    await ctx["qdrant"].close()
    await ctx["neo4j"].close()
    await ctx["engine"].dispose()


class WorkerSettings:
    """arq worker entrypoint — ``uv run poe worker`` /
    ``arq api.workers.build_worker.WorkerSettings``."""

    functions = [run_build_task]
    # BA2d-3 crash-recovery reaper: twice a minute (~1-min recovery given the 60s
    # lease TTL), re-enqueue builds whose worker crashed. unique=True → one worker
    # runs it per tick; a short timeout keeps it off the generous build job_timeout;
    # max_tries=1 (the default) — a failed tick just retries on the next one.
    cron_jobs = [cron(reap_stuck_builds, second={0, 30}, unique=True, timeout=30)]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings()
    # job_timeout is a GENEROUS hung-build backstop, not the crash-recovery timer:
    # arq cancels a job that outruns it (via asyncio.wait_for) and does NOT retry
    # the TimeoutError, which would strand the SoR jobs row non-terminal, so it must
    # exceed any legitimately-slow build. Crash recovery is decoupled — the BA2d-3
    # lease reaper re-enqueues a `building` build whose lease expired within ~a
    # minute. See core.config.build_job_timeout_seconds.
    job_timeout = get_settings().build_job_timeout_seconds
    max_tries = 3
