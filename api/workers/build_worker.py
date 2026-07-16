"""BA2d-2 arq build worker — Redis queue → the §5 build pipeline.

DESIGN §11 runs a separate ``worker`` container that consumes the jobs queue and
executes builds; the API (BA2e) only enqueues. This wires arq + Redis onto core:

* ``on_startup`` builds ONE long-lived dep bundle (engine + Qdrant/Neo4j/LLM/
  embedder — the ``ProjectContext`` shape, pooled and reused across jobs) plus a
  unique owner id for this worker process.
* ``run_build_task`` runs the WHOLE dispatch under the BA2d-1 execution lease
  (``job_lease`` entered before preflight, so a crash anywhere mid-dispatch is
  reaper-visible, and a duplicate dispatch is a no-op rather than a second
  concurrent execution), reuses the build's pinned config (``create_job``
  snapshots it at job creation; reused on every dispatch so neither a queue-delay
  config edit nor a re-dispatch can drift a resume), builds the six §5 stages off
  the bundle, and runs ``run_build``.
* ``enqueue_build`` (BA2e's trigger calls it after ``create_job``) enqueues with
  ``_job_id=str(job_id)`` for arq's own dispatch dedup.
* ``reap_stuck_builds`` (a cron, BA2d-3) re-enqueues builds whose worker crashed —
  found by an expired execution lease — so crash recovery is fast (~1 min) and
  decoupled from arq's generous job_timeout; the DB lease keeps it a single
  executor. This makes the BA2d-1 heartbeat-lease the sole build-liveness authority.
  BA2e adds a second sweep to the same cron: ``queued`` jobs that never acquired a
  lease past a grace period (a trigger's lost enqueue — the class-12 crash window
  before any lease exists) get the trigger's enqueue replayed.

Two dedup layers, by design: arq's ``_job_id`` refuses to *enqueue* a duplicate
while one is queued/running (the cheap first line); the DB heartbeat-lease is the
crash-safe backstop for the *execution* itself (a worker that dies mid-build has
its lease expire so the job is reclaimable). The worker never trusts arq's own
job status — the ``jobs`` row is the SoR (§27.7).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from arq import cron
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import BuildConfigError, load_build_config
from core.builds.lease import job_lease
from core.builds.orchestrator import BuildNotResumableError, run_build
from core.builds.stages import default_stages
from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.paths import safe_project_subdir
from core.registry import (
    capture_config_snapshot,
    find_reapable_jobs,
    find_unenqueued_jobs,
    get_eval_inputs_fingerprint,
    get_project,
    holds_lease,
    is_active_status,
    is_cancel_requested,
    lock_job,
    set_progress,
)
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

logger = logging.getLogger(__name__)

#: arq task names — enqueue and the WorkerSettings registration must agree, so the
#: strings are defined once (arq registers a plain coroutine under its __name__).
BUILD_TASK = "run_build_task"
EVAL_TASK = "run_eval_task"

#: The ``jobs.kind`` the reaper re-dispatches onto ``EVAL_TASK`` (with the job's
#: ``build_id``) instead of ``BUILD_TASK``. build/ingest jobs both run the §5
#: pipeline via ``BUILD_TASK``; an eval job is the one kind that maps elsewhere, so
#: crash/lost-dispatch recovery must branch on it. The eval trigger stamps this
#: same string (api.routers.builds), so creator and reaper can't drift.
EVAL_JOB_KIND = "eval"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def enqueue_build(redis: ArqRedis, project: str, job_id: uuid.UUID) -> bool:
    """Enqueue a build for the worker; True if a new dispatch was enqueued,
    False if arq dedup-refused it. ``_job_id=str(job_id)`` gives arq's own
    dispatch dedup — it refuses to enqueue a duplicate while the job is
    queued/running — the cheap first line of defense; the DB execution lease is
    the crash-safe backstop. BA2e's trigger endpoint calls this after create_job;
    the reaper's queued-sweep re-runs the exact same call for a job whose
    original enqueue was lost (see ``find_unenqueued_jobs``).
    """
    job = await redis.enqueue_job(BUILD_TASK, project, str(job_id), _job_id=str(job_id))
    return job is not None


async def enqueue_eval(
    redis: ArqRedis, project: str, job_id: uuid.UUID, build_id: uuid.UUID
) -> bool:
    """Enqueue an eval job (UXC1b); True if a new dispatch was enqueued, False if
    arq dedup-refused it. ``_job_id=str(job_id)`` gives arq's own dispatch dedup,
    same as ``enqueue_build``; the DB execution lease is the crash-safe backstop.
    The eval endpoint calls this after ``create_job(kind="eval", build_id=…)``."""
    job = await redis.enqueue_job(
        EVAL_TASK, project, str(job_id), str(build_id), _job_id=str(job_id)
    )
    return job is not None


async def run_eval_task(
    ctx: dict[str, Any], project: str, job_id: str, build_id: str
) -> str | None:
    """arq task: run the project's golden set against a NAMED build under the
    execution lease, writing the report to ``builds.eval`` (the same core path
    ``cli/main.py eval`` walks — DR-010). Returns the terminal job status, or
    None if a live peer already holds the lease (a deliberate no-op dispatch).

    The golden set and query policy are the project's on-disk config
    (``<projects_dir>/<project>/eval/…`` — CFG1 will unify this with the
    ``projects.config`` registry). ANY failure once the job is marked ``running``
    — a deterministic refusal (missing/invalid golden or policy, a vanished build,
    an unconfigured model) OR a store/infra outage (Neo4j/Qdrant/Postgres-read) —
    is recorded terminal on the durable jobs row and NOT retried. This mirrors
    ``run_build``'s stage boundary (§22): an eval must never strand the row
    ``running``+unleased, which NO sweep recovers (``find_reapable_jobs`` needs a
    held lease; ``find_unenqueued_jobs`` needs ``queued``) and would lock the
    project out of every future job via ``create_job_exclusive``. The caller
    re-runs — eval is idempotent (it just re-writes ``builds.eval``)."""
    from core.eval.golden import load_golden
    from core.eval.idempotency import eval_inputs_fingerprint
    from core.eval.inputs import eval_input_paths
    from core.eval.runner import models_needed, persist_build_eval, run_eval
    from core.mcp.policy import load_query_policy

    engine = ctx["engine"]
    eval_job = uuid.UUID(job_id)
    target_build = uuid.UUID(build_id)
    async with job_lease(engine, eval_job, ctx["owner"]) as acquired:
        if not acquired:
            return None  # a live peer is executing this job — deliberate no-op
        # Path-safety BEFORE reading any on-disk config: the project name is a
        # path component of projects_dir, but it is only length-validated — a name
        # like '..' would read <projects_dir>/../{eval/golden.yaml,config.yaml},
        # OUTSIDE the projects root. The same guard the upload corpus uses; an
        # unsafe name is a deterministic refusal (a retry can't fix it).
        root = safe_project_subdir(Path(get_settings().projects_dir), project)
        if root is None:
            await _fail_job(
                engine,
                eval_job,
                ValueError(f"project {project!r} is not a valid projects-dir path component"),
            )
            return "failed"
        # Terminal-job + cancellation checkpoint BEFORE starting work, from ONE locked
        # read. Acquiring the execution lease means "no LIVE peer" — but the lease is
        # status-blind, so a stale-lease reaper race can hand this row over already
        # terminal: it enqueues a replacement while the original worker is only STARVED,
        # then the original finishes (status → done/failed/cancelled) and RELEASES its
        # lease, so this replacement acquires the now-terminal row. Re-running would
        # reopen a finished eval and OVERWRITE builds.eval (the §20 gate's input) with a
        # second computation. So LOCK the row (the same FOR UPDATE the finalize/reaper
        # use — class-10: the decisive read belongs under the write's lock, never a
        # prior unlocked SELECT) and, if it is no longer queued/running, do no work: a
        # benign no-op mirroring run_build's BuildNotResumableError. None also covers the
        # project being deleted out from under us — equally nothing to do.
        async with engine.begin() as conn:
            job = await lock_job(conn, eval_job)
            if job is None or not is_active_status(job.status):
                return None
            # Cancellation checkpoint under the SAME lock (mirrors the finalize's
            # lock_job → is_cancel_requested): /jobs/{id}/cancel flags cancel_requested
            # cooperatively (like a build's between-stage checks), so an eval accepted
            # for cancellation must not run to completion and report success —
            # terminalize it 'cancelled' and do no work.
            if await is_cancel_requested(conn, eval_job):
                await set_progress(conn, eval_job, status="cancelled", finished_at=sa.func.now())
                return "cancelled"
        try:
            # Drift guard (UXC1b): the endpoint pinned the ACCEPT-time fingerprint of the
            # golden set + query policy on the job. If a user edited either between the 202
            # and this dispatch, scoring the new bytes would return a report for inputs the
            # client never accepted — the idempotency key is scoped to the OLD fingerprint.
            # Re-fingerprint the live inputs and fail loud on a mismatch (a stated failure
            # the client re-triggers). A job with NO pin (created before the pin existed)
            # skips the check. This runs inside preflight so a mismatch terminalizes the
            # job like any other deterministic refusal, never strands it.
            async with engine.connect() as conn:
                pinned_fingerprint = await get_eval_inputs_fingerprint(conn, eval_job)
            if pinned_fingerprint is not None:
                live_fingerprint = eval_inputs_fingerprint(
                    Path(get_settings().projects_dir), project
                )
                if live_fingerprint != pinned_fingerprint:
                    raise ValueError(
                        "eval inputs (golden set / query policy) changed since this eval "
                        "was accepted; re-trigger it so the report scores the intended inputs"
                    )
            golden_path, policy_path = eval_input_paths(root)
            golden = load_golden(golden_path)
            policy = load_query_policy(policy_path)
            needs_embedder, needs_llm = models_needed(golden, policy)
            embedder = ctx["embedder"] if needs_embedder else None
            llm = ctx["llm"] if needs_llm else None
        except Exception as exc:  # noqa: BLE001 — preflight is ALL-LOCAL (read+parse+validate the golden set / policy, pick models). GoldenError/PolicyError/LLMNotConfiguredError are the expected refusals, but a bad golden/policy PATH is equally deterministic: a directory or bad perms raises OSError, invalid UTF-8 raises UnicodeDecodeError, malformed YAML its own error. ANY escape here strands the job 'queued'+unleased (job_lease's finally released the lease before the 'running' mark), which the queued-sweep replays FOREVER and create_job_exclusive uses to block every later job for the project. So terminalize like the run phase below, never propagate. (CancelledError is BaseException → still propagates to job_lease, which on cancel leaves the lease HELD so the reaper reclaims — no strand.)
            await _fail_job(engine, eval_job, exc)
            return "failed"
        async with engine.begin() as conn:
            await set_progress(conn, eval_job, status="running", progress=0.0)
        try:
            # persist=False: run_eval COMPUTES the report but does NOT write builds.eval.
            # The worker commits it below, inside the finalize txn under the cancel
            # cutoff, so a cancelled eval never commits a report the §20 gate could read.
            async with engine.connect() as conn, ctx["neo4j"].session() as session:
                report = await run_eval(
                    conn,
                    ctx["qdrant"],
                    session,
                    embedder,
                    llm,
                    project,
                    target_build,
                    golden,
                    policy,
                    persist=False,
                )
        except Exception as exc:  # noqa: BLE001 — the eval boundary, mirroring run_build's stage boundary: ANY error once 'running' (a refusal like a vanished build / unconfigured model, OR a store outage) must terminalize the jobs row, never leave it 'running'+unleased for no sweep to recover. (CancelledError is BaseException → still propagates to job_lease, which on cancel leaves the lease HELD so the reaper reclaims the timed-out job — never a 'running'+unleased strand; same mechanism for run_build.) run_eval can be LONG (LLM calls), so the lease can lapse here and the reaper hand off to a replacement — _fail_eval terminalizes ONLY if we still lead, never overwriting the new holder's result with this stale worker's 'failed'.
            return await _fail_eval(engine, eval_job, ctx["owner"], exc)
        # Finalize honoring BOTH a cancel that arrived DURING the run AND a lease
        # handoff to a replacement, mirroring run_build's terminalize (orchestrator §5):
        # LOCK the job row FOR UPDATE, then under that lock verify this worker still
        # LEADS — the job is live AND we still own the lease (_eval_leads). run_eval can
        # be long, so a heartbeat-starved lapse can let the reaper hand execution to a
        # replacement that terminalizes the job; a stale worker persisting anyway would
        # overwrite the new holder's builds.eval/status (or hide its failure) with stale
        # inputs. If we no longer lead → a benign no-op (None), the replacement is
        # authoritative. Otherwise read cancel_requested UNDER the same lock (the class-10
        # cutoff — a cancel racing the finalize blocks on the lock and is cleanly rejected
        # by request_cancel's status-guard) and commit builds.eval ONLY when not cancelled
        # and in the SAME txn, so the §20 gate never observes a withheld/stale report.
        try:
            async with engine.begin() as conn:
                if not await _eval_leads(conn, eval_job, ctx["owner"]):
                    return None
                cancelled = await is_cancel_requested(conn, eval_job)
                if not cancelled:
                    await persist_build_eval(conn, report)
                status = "cancelled" if cancelled else "done"
                await set_progress(
                    conn, eval_job, status=status, progress=1.0, finished_at=sa.func.now()
                )
        except Exception as exc:  # noqa: BLE001 — a persist/finalize failure (e.g. the build pruned mid-eval → LookupError from persist_build_eval, or a store blip) must terminalize the job, never leave it 'running'+unleased. The begin() rolled back (no half report, no finalized job); _fail_eval records it in its own txn, again only if we still lead. Mirrors the eval boundary above.
            return await _fail_eval(engine, eval_job, ctx["owner"], exc)
        return status


async def reenqueue_build(
    redis: ArqRedis, project: str, job_id: uuid.UUID, *, stale_expiry: datetime
) -> bool:
    """Re-dispatch a crashed build (BA2d-3 reaper); True if a new dispatch was
    enqueued, False if one is already pending. The arq id is DETERMINISTIC per
    stale lease — ``reap:<job>:<expiry>`` — not the job's own id (the crashed
    dispatch's in-progress key lingers for job_timeout+10s, so a same-id enqueue
    would be refused for 24h) and not a fresh id per call (the row keeps matching
    ``find_reapable_jobs`` until the replacement actually STARTS, so a fresh id
    every 30s tick would pile up unbounded duplicates behind a saturated queue).
    The stale expiry only moves on acquire/renew, so all ticks while the job sits
    crashed derive the SAME id and arq refuses the duplicates — one pending
    recovery per stale lease. If the replacement itself crashes, the lease it
    acquired expires at a NEW timestamp → a new id → the next recovery generation;
    if it fails BEFORE ever acquiring (same expiry → same id), keep_result=0 (see
    WorkerSettings) frees the id the moment it finishes, so the next tick retries.
    The DB execution lease remains the single-executor guarantee either way."""
    job = await redis.enqueue_job(
        BUILD_TASK,
        project,
        str(job_id),
        _job_id=f"reap:{job_id}:{stale_expiry.isoformat()}",
    )
    return job is not None


async def reenqueue_eval(
    redis: ArqRedis,
    project: str,
    job_id: uuid.UUID,
    build_id: uuid.UUID,
    *,
    stale_expiry: datetime,
) -> bool:
    """Re-dispatch a crashed eval (BA2d-3 reaper), the eval-task sibling of
    ``reenqueue_build``: same deterministic per-stale-lease arq id
    (``reap:<job>:<expiry>`` — one pending recovery per stale lease, not one per
    tick), onto ``EVAL_TASK`` with the job's target ``build_id`` so recovery
    resumes the eval instead of mis-running it as a build. The DB execution lease
    stays the single-executor guarantee."""
    job = await redis.enqueue_job(
        EVAL_TASK,
        project,
        str(job_id),
        str(build_id),
        _job_id=f"reap:{job_id}:{stale_expiry.isoformat()}",
    )
    return job is not None


async def reap_stuck_builds(ctx: dict[str, Any]) -> int:
    """BA2d-3 cron: re-enqueue builds whose worker crashed, decoupling crash
    recovery from arq's (now generous) job_timeout.

    A crashed/starved worker stops heartbeating, so its job's execution lease
    expires on the DB clock. This finds those (``find_reapable_jobs`` — expired
    held lease + non-terminal job) and re-enqueues each under a deterministic
    per-stale-lease arq id, so a fresh dispatch reclaims the now-free lease and
    resumes (~1-min recovery vs the 24h backstop) while re-ticks over the same
    stale lease dedup instead of piling up duplicates. A second sweep (BA2e)
    covers the window BEFORE any lease exists: ``queued`` jobs that never
    acquired one past the enqueue grace (``find_unenqueued_jobs``) get the
    trigger's enqueue replayed under the job's own arq id. Both sweeps dispatch
    by ``kind`` — build/ingest onto ``BUILD_TASK``, eval onto ``EVAL_TASK`` with
    its target build_id — so a crashed eval resumes as an eval. An idle
    tick is a no-op; ``unique=True`` (see WorkerSettings) runs this on one
    worker per tick. Returns the number of NEW dispatches enqueued
    (dedup-suppressed ones don't count)."""
    engine: AsyncEngine = ctx["engine"]
    redis: ArqRedis = ctx["redis"]
    async with engine.connect() as conn:
        reapable = await find_reapable_jobs(conn)
        unenqueued = await find_unenqueued_jobs(conn, get_settings().job_enqueue_grace_seconds)
    enqueued = 0
    for job_id, project, kind, build_id, stale_expiry in reapable:
        if kind == EVAL_JOB_KIND:
            # an eval resumes as an eval — never mis-recovered as a build; the
            # jobs row carries the target build_id (create_job_exclusive stamps it)
            if not _reap_eval_ok(job_id, build_id):
                continue
            assert build_id is not None
            dispatched = await reenqueue_eval(
                redis, project, job_id, build_id, stale_expiry=stale_expiry
            )
        else:
            dispatched = await reenqueue_build(redis, project, job_id, stale_expiry=stale_expiry)
        if dispatched:
            enqueued += 1
    # BA2e queued-sweep: a job whose trigger-time enqueue was lost (class-12
    # window BEFORE any lease exists — invisible to the expired-lease sweep
    # above) gets the trigger's lost step replayed verbatim; the two predicates
    # are disjoint on lease_owner, so no job is swept twice. A job merely
    # backlogged past the grace is dedup-refused under its own arq id (no-op).
    for job_id, project, kind, build_id in unenqueued:
        if kind == EVAL_JOB_KIND:
            if not _reap_eval_ok(job_id, build_id):
                continue
            assert build_id is not None
            dispatched = await enqueue_eval(redis, project, job_id, build_id)
        else:
            dispatched = await enqueue_build(redis, project, job_id)
        if dispatched:
            enqueued += 1
    if reapable or unenqueued:
        logger.info(
            "reaper: %d stuck + %d unenqueued job(s), %d newly (re-)dispatched: %s",
            len(reapable),
            len(unenqueued),
            enqueued,
            [str(j) for j, *_ in reapable] + [str(j) for j, *_ in unenqueued],
        )
    return enqueued


def _reap_eval_ok(job_id: uuid.UUID, build_id: uuid.UUID | None) -> bool:
    """An eval job with no ``build_id`` can't be re-dispatched (``EVAL_TASK`` needs
    its target) — a contradiction ``create_job_exclusive(kind="eval", build_id=…)``
    forbids, but if one ever appears, skip+log it rather than crash the whole reaper
    tick (which would strand every other stuck job)."""
    if build_id is None:
        logger.error("reaper: eval job %s has no build_id; cannot re-dispatch", job_id)
        return False
    return True


async def run_build_task(ctx: dict[str, Any], project: str, job_id: str) -> str | None:
    """arq task: run one build under the execution lease.

    ENTERS THE LEASE FIRST — before preflight/stage construction — so the lease
    brackets the entire dispatch: a worker that crashes anywhere mid-dispatch
    (even during preflight) leaves a held-but-lapsing lease the BA2d-3 reaper can
    see, instead of an unmarked ``queued`` row stranded until arq's 24h timeout.
    Then pins+loads the project's config (see the preflight comment), builds the
    six §5 stages off the ``ctx`` dep bundle, and runs ``run_build``. Returns the
    terminal build status, or ``None`` if a live peer already holds the lease
    (this dispatch was a deliberate no-op). A neo4j session is opened per job
    (the driver is shared); ``run_build`` opens its own per-stage Postgres
    transactions off the engine.
    """
    engine = ctx["engine"]
    build_job = uuid.UUID(job_id)
    async with job_lease(engine, build_job, ctx["owner"]) as acquired:
        if not acquired:
            return None  # a live peer is executing this job — deliberate no-op
        # Preflight (project existence + config) happens BEFORE run_build enters the
        # orchestrator path that marks jobs.status. A deterministic failure here
        # (vanished project / malformed config) would otherwise leave the durable
        # jobs row queued forever — blocking project delete and misleading GET /jobs
        # — so record it on the row and don't retry (a retry can't fix it).
        # Transient errors (e.g. a DB blip) are NOT caught, so arq still retries.
        #
        # The config is PINNED to the build: create_job snapshots proj.config onto
        # the job at creation, and capture_config_snapshot reads that pinned config
        # back on every dispatch (defensively pinning live config if a job somehow
        # lacks one), so neither a PATCH /projects during the queue delay nor a
        # re-dispatch (an arq retry, or the BA2d-3 reaper) can drift a resuming
        # build's chunking/ontology params. The defensive pin can write the jobs
        # row, so this runs in a committing begin().
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
                outcome = await run_build(engine, project, build_job, stages)
            except BuildNotResumableError:
                # Benign recovery race: a re-dispatch (an arq retry, or the BA2d-3
                # reaper) acquired the lease AFTER the original — starved, not dead —
                # worker terminalized the build and released it. run_build's
                # FOR-UPDATE-locked build-status check is the atomic recheck: it
                # found the build already terminal, so recovery wasn't needed. This
                # dispatch is a no-op, not a failure — don't manufacture a
                # failed/retried arq job for a build that already succeeded (the
                # jobs row is already terminal, set by that worker).
                return None
        return outcome.status


def _failure_error(exc: Exception) -> dict[str, Any]:
    """The FULL frozen Error shape (§27.2) for a worker-side terminal failure. §27.2
    requires request_id; no HTTP request exists here, so the fresh id names this
    failure record."""
    return {
        "code": "INTERNAL",
        "message": str(exc),
        "details": None,
        "request_id": str(uuid.uuid4()),
    }


async def _fail_job(engine: AsyncEngine, job_id: uuid.UUID, exc: Exception) -> None:
    """Record a preflight failure on the durable jobs row (its own committed
    transaction), so a build that never reached the orchestrator still terminates
    the job instead of leaving it queued. Used for failures BEFORE the 'running'
    mark, where this worker just acquired the lease and no replacement can exist —
    so it is unconditional (the eval RUN/finalize failures use _fail_eval, which is
    lease-guarded, because the lease can lapse across their long window)."""
    async with engine.begin() as conn:
        await set_progress(
            conn, job_id, status="failed", finished_at=sa.func.now(), error=_failure_error(exc)
        )


async def _eval_leads(conn: AsyncConnection, eval_job: uuid.UUID, owner: str) -> bool:
    """Under the job row lock (the write cutoff), whether THIS worker still LEADS the
    eval: the job is still live AND we still own its execution lease. A heartbeat lapse
    can let the reaper hand execution to a replacement; a stale worker that writes
    anyway would clobber the new holder's builds.eval/status. lock_job (FOR UPDATE)
    serializes a concurrent acquire_lease behind it (class-10: the decisive read lives
    under the write's row lock, never a prior unlocked SELECT)."""
    job = await lock_job(conn, eval_job)
    return (
        job is not None
        and is_active_status(job.status)
        and await holds_lease(conn, eval_job, owner)
    )


async def _fail_eval(
    engine: AsyncEngine, eval_job: uuid.UUID, owner: str, exc: Exception
) -> str | None:
    """Terminalize a failed eval 'failed' as _fail_job does, but ONLY if this worker
    still LEADS (live + lease-owning), re-checked under the job row lock. run_eval and
    the finalize span a long window in which the lease can lapse and the reaper hand
    off to a replacement; a stale worker must not overwrite the replacement's result or
    hide its failure with this worker's 'failed'. Returns 'failed' if it recorded the
    failure, or None (a benign no-op) if a replacement had taken over."""
    async with engine.begin() as conn:
        if not await _eval_leads(conn, eval_job, owner):
            return None
        await set_progress(
            conn, eval_job, status="failed", finished_at=sa.func.now(), error=_failure_error(exc)
        )
    return "failed"


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

    functions = [run_build_task, run_eval_task]
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
    # No arq results, ever: the jobs row is the SoR (nothing consumes arq's result
    # payloads), and a kept result RESERVES its custom job id for keep_result
    # seconds (default 3600). That reservation would break the reaper: a
    # replacement dispatch that fails BEFORE acquiring the lease (e.g. a transient
    # Postgres outage in job_lease entry) leaves the stale expiry — and thus the
    # derived reap id — unchanged, so its kept result would block that id for an
    # hour and stall recovery. keep_result=0 frees the id the moment the failed
    # dispatch finishes, so the next 30s tick retries.
    keep_result = 0
