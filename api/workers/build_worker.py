"""BA2d-2 arq build worker — Redis queue → the §5 build pipeline.

DESIGN §11 runs a separate ``worker`` container that consumes the jobs queue and
executes builds; the API (BA2e) only enqueues. This wires arq + Redis onto core:

* ``on_startup`` builds ONE long-lived dep bundle (engine + Qdrant/Neo4j/LLM/
  embedder — the ``ProjectContext`` shape, pooled and reused across jobs) plus a
  unique owner id for this worker process.
* ``run_build_task`` loads a job's project config, builds the six §5 stages off
  the bundle, and executes under the BA2d-1 execution lease (``run_build_leased``)
  so a duplicate dispatch of the same job is a no-op rather than a second
  concurrent execution.
* ``enqueue_build`` (BA2e's trigger calls it after ``create_job``) enqueues with
  ``_job_id=str(job_id)`` for arq's own dispatch dedup.

Two dedup layers, by design: arq's ``_job_id`` refuses to *enqueue* a duplicate
while one is queued/running (the cheap first line); the DB heartbeat-lease is the
crash-safe backstop for the *execution* itself (a worker that dies mid-build has
its lease expire so the job is reclaimable). The worker never trusts arq's own
job status — the ``jobs`` row is the SoR (§27.7).
"""

from __future__ import annotations

import uuid
from typing import Any

from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import load_build_config
from core.builds.lease import run_build_leased
from core.builds.stages import default_stages
from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.registry import get_project
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

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


async def run_build_task(ctx: dict[str, Any], project: str, job_id: str) -> str | None:
    """arq task: run one build under the execution lease.

    Reads the long-lived deps from ``ctx`` (built in ``on_startup``), loads the
    project's config, builds the six §5 stages, and executes via
    ``run_build_leased`` with this worker's owner id. Returns the terminal build
    status, or ``None`` if a live peer already holds the lease (this dispatch was
    a deliberate no-op). A neo4j session is opened per job (the driver is shared);
    ``run_build`` opens its own per-stage Postgres transactions off the engine.
    """
    engine = ctx["engine"]
    async with engine.connect() as conn:
        proj = await get_project(conn, project)
    if proj is None:  # a build for a vanished project must fail loud, not silently no-op
        raise LookupError(f"project {project!r} does not exist")
    config = load_build_config(proj.config)
    async with ctx["neo4j"].session() as session:
        stages = default_stages(
            config,
            chat_model=ctx["llm"],
            embedder=ctx["embedder"],
            vector_client=ctx["qdrant"],
            graph_session=session,
        )
        outcome = await run_build_leased(
            engine, project, uuid.UUID(job_id), stages, owner=ctx["owner"]
        )
    return outcome.status if outcome is not None else None


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
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings()
    # A build can be long (LLM extraction over many docs). The DB heartbeat-lease
    # (BA2d-1), NOT this timeout, is the liveness mechanism, so give jobs generous
    # headroom; arq retries a timed-out/crashed job and run_build resumes the
    # still-building build.
    job_timeout = 3600
    max_tries = 3
