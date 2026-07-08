"""BA2d-1 execution lease — the single-executor guard around ``run_build``.

``run_build``'s FOR UPDATE lock serializes build *creation* but releases at the
build-resolution commit; two dispatches of one job then run all six stages
concurrently against the same building build. Convergent idempotency keeps that
SAFE (each stage re-reads the SoR and skips done work) but doubles the LLM cost
and races the derived-store writes. This lease adds the missing *execution*
mutual-exclusion: a worker claims a DB lease on the job, heartbeats it while
run_build runs, and releases it at the end. A crashed holder stops heartbeating,
its lease expires on the DB clock, and the next dispatch reclaims and resumes —
so a lost worker never strands the job (the distinction TASKS.md draws between
"actively running" and "crashed running").

The lease is a *liveness* layer over the idempotent-*safety* floor: in the
common case exactly one worker executes; if a heartbeat is ever starved past the
TTL, execution degrades to concurrent-but-safe, never to corruption.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncEngine

from core.builds.orchestrator import BuildOutcome, Stages, run_build
from core.registry.jobs import acquire_lease, release_lease, renew_lease

#: Lease lifetime and heartbeat cadence. The heartbeat renews well within the TTL
#: (3× per window) so one slow renewal doesn't drop the lease; the TTL rides out
#: a GC pause / brief DB blip but is short enough that a crashed worker's job is
#: reclaimable promptly.
_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_HEARTBEAT_SECONDS = 20.0


async def _heartbeat(
    engine: AsyncEngine, job_id: uuid.UUID, owner: str, ttl_seconds: float, interval: float
) -> None:
    """Renew the lease every ``interval`` seconds until cancelled. If a renewal
    finds the lease is no longer ours (an expiry-reclaim handed it off), stop: the
    peer now owns execution and our run_build continues only on the convergent-
    idempotency floor (safe, just no longer deduped)."""
    while True:
        await asyncio.sleep(interval)
        try:
            async with engine.begin() as conn:
                still_ours = await renew_lease(conn, job_id, owner, ttl_seconds)
        except Exception:  # noqa: BLE001 — a transient DB blip must not kill the heartbeat nor, surfacing from `await beat` in the finally, mask the build result; skip this beat and retry. Persistent failure just lapses the lease on the DB clock → a peer reclaims (the safe fallback). (CancelledError is BaseException, so cancel still ends the loop.)
            continue
        if not still_ours:
            return


async def run_build_leased(
    engine: AsyncEngine,
    project: str,
    job_id: uuid.UUID,
    stages: Stages,
    *,
    owner: str,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
    build_id: uuid.UUID | None = None,
    config_hash: str | None = None,
    source_hash: str | None = None,
    step_failure_ratio: float | None = None,
) -> BuildOutcome | None:
    """Run a build under an execution lease held by ``owner``.

    Returns the ``BuildOutcome`` if this call acquired the lease and ran the
    pipeline, or ``None`` if another live worker already holds it — then this
    dispatch is a deliberate no-op (the peer is executing the same build). While
    run_build runs, a background heartbeat renews the lease so a long build keeps
    it; the lease is always released on exit (success OR failure) so a retry can
    re-acquire. A crashed worker never releases, but its lease expires on the DB
    clock and the next dispatch reclaims it.
    """
    async with engine.begin() as conn:
        acquired = await acquire_lease(conn, job_id, owner, ttl_seconds)
    if not acquired:
        # a live peer holds the lease → this dispatch is a deliberate no-op. (An
        # absent job also fails to acquire and returns None rather than raising,
        # but the delete-project guard refuses deletion under an active job, so a
        # dispatched job can't vanish — that path is unreachable in practice.)
        return None
    beat = asyncio.create_task(_heartbeat(engine, job_id, owner, ttl_seconds, heartbeat_seconds))
    try:
        return await run_build(
            engine,
            project,
            job_id,
            stages,
            build_id=build_id,
            config_hash=config_hash,
            source_hash=source_hash,
            step_failure_ratio=step_failure_ratio,
        )
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
        async with engine.begin() as conn:
            await release_lease(conn, job_id, owner)
