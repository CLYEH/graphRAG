"""Jobs registry access (BA2a) — the control-plane CRUD behind long operations.

Plain async functions over an ``AsyncConnection``, the same non-build-scoped
face as ``core.registry.store``. A ``jobs`` row is the durable SoR the Console
serves for GET /jobs/{id}; the arq worker mutates it as the pipeline runs
(``set_progress``), and the API reads it. Cancellation is cooperative: the
cancel endpoint flips ``cancel_requested`` and the orchestrator checks
``is_cancel_requested`` between steps. No HTTP concerns here — the router (BA2d)
owns the §15 envelope and the contract Job serialization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables

#: Live statuses — a job in one of these is still doing work (a project can't be
#: deleted under it, and cancel is still meaningful).
_ACTIVE_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class Job:
    """A long-operation tracking row. ``id`` is the job id used in API paths;
    ``build_id`` is the building build this job writes to (null until the
    orchestrator resolves it); ``error`` is the §15 Error shape or None."""

    id: uuid.UUID
    project: str
    kind: str
    build_id: uuid.UUID | None
    status: str
    step: str | None
    progress: float
    message: str | None
    error: dict[str, Any] | None
    cancel_requested: bool
    created_at: datetime
    finished_at: datetime | None


class JobNotFoundError(Exception):
    """A job id that does not exist (GET/cancel on an unknown job)."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"job {job_id} does not exist")
        self.job_id = job_id


class _Unset:
    """Partial-update sentinel — 'field omitted' vs 'set to this value'."""


_UNSET: Any = _Unset()

_COLS = (
    tables.jobs.c.id,
    tables.jobs.c.project,
    tables.jobs.c.kind,
    tables.jobs.c.build_id,
    tables.jobs.c.status,
    tables.jobs.c.step,
    tables.jobs.c.progress,
    tables.jobs.c.message,
    tables.jobs.c.error,
    tables.jobs.c.cancel_requested,
    tables.jobs.c.created_at,
    tables.jobs.c.finished_at,
)


async def create_job(
    conn: AsyncConnection, project: str, kind: str, *, build_id: uuid.UUID | None = None
) -> Job:
    """Insert a fresh ``queued`` job for a project. The caller (a trigger
    endpoint) has already verified the project exists; the FK backstops a
    concurrent delete.

    ``config_snapshot`` is pinned to the project config AS OF NOW (a scalar
    subquery), so the build runs the config the user submitted even if a later
    ``PATCH /projects`` edits it during the queue delay before the first dispatch.
    The worker reuses this snapshot on every (re-)dispatch (capture_config_snapshot)
    rather than re-reading live config, which would drift a resuming build."""
    row = (
        await conn.execute(
            tables.jobs.insert()
            .values(
                project=project,
                kind=kind,
                build_id=build_id,
                config_snapshot=(
                    sa.select(tables.projects.c.config)
                    .where(tables.projects.c.name == project)
                    .scalar_subquery()
                ),
            )
            .returning(*_COLS)
        )
    ).one()
    return Job(*row)


async def get_job(conn: AsyncConnection, job_id: uuid.UUID) -> Job | None:
    row = (await conn.execute(sa.select(*_COLS).where(tables.jobs.c.id == job_id))).one_or_none()
    return Job(*row) if row is not None else None


async def lock_job(conn: AsyncConnection, job_id: uuid.UUID) -> Job | None:
    """`SELECT … FOR UPDATE` the job row and return it (or None if absent).
    Serializes concurrent workers dispatched the same job while they resolve
    its build (the orchestrator): a second worker blocks here until the first
    commits, then re-reads the now-attached ``build_id`` instead of minting a
    second build. Caller must hold an open transaction (the lock lives until it
    commits/rolls back)."""
    row = (
        await conn.execute(sa.select(*_COLS).where(tables.jobs.c.id == job_id).with_for_update())
    ).one_or_none()
    return Job(*row) if row is not None else None


async def set_progress(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    *,
    status: str | _Unset = _UNSET,
    step: str | None | _Unset = _UNSET,
    progress: float | _Unset = _UNSET,
    message: str | None | _Unset = _UNSET,
    build_id: uuid.UUID | None | _Unset = _UNSET,
    error: dict[str, Any] | None | _Unset = _UNSET,
    finished_at: datetime | sa.ColumnElement[Any] | _Unset = _UNSET,
) -> Job | None:
    """Patch a job's live fields (only the ones passed change). Returns the
    updated job, or None if the id is unknown. The worker calls this on its own
    short-lived, immediately-committed connection so a GET /jobs poller sees
    progress without waiting on the pipeline's long transaction."""
    values = {
        col: val
        for col, val in (
            ("status", status),
            ("step", step),
            ("progress", progress),
            ("message", message),
            ("build_id", build_id),
            ("error", error),
            ("finished_at", finished_at),
        )
        if not isinstance(val, _Unset)
    }
    if not values:
        return await get_job(conn, job_id)
    row = (
        await conn.execute(
            tables.jobs.update()
            .where(tables.jobs.c.id == job_id)
            .values(**values)
            .returning(*_COLS)
        )
    ).one_or_none()
    return Job(*row) if row is not None else None


async def request_cancel(conn: AsyncConnection, job_id: uuid.UUID) -> Job:
    """Flag a job for cooperative cancellation. Sets ``cancel_requested`` only
    while the job is still active (a terminal job is left untouched). Raises
    JobNotFoundError for an unknown id. Returns the job's CURRENT state — the
    worker does the actual stopping at the next step boundary."""
    job = await get_job(conn, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if job.status in _ACTIVE_STATUSES and not job.cancel_requested:
        # Status-guard the UPDATE, not just the get_job read above: the
        # orchestrator's terminalization holds FOR UPDATE on this row and reads
        # cancel_requested under it, so a cancel racing that finalize blocks
        # here and then finds a terminal job — the guard makes it a clean no-op
        # instead of flagging an already-finished job (the class-10 lesson: the
        # unlocked read is TOCTOU; the decisive check belongs in the write).
        await conn.execute(
            tables.jobs.update()
            .where(tables.jobs.c.id == job_id, tables.jobs.c.status.in_(_ACTIVE_STATUSES))
            .values(cancel_requested=True)
        )
        return await get_job(conn, job_id) or job
    return job


async def is_cancel_requested(conn: AsyncConnection, job_id: uuid.UUID) -> bool:
    """Whether a cancel has been requested — the orchestrator's between-steps
    checkpoint."""
    return bool(
        (
            await conn.execute(
                sa.select(tables.jobs.c.cancel_requested).where(tables.jobs.c.id == job_id)
            )
        ).scalar_one_or_none()
    )


# ── Execution lease (BA2d) ──────────────────────────────────────────────────
# run_build's FOR UPDATE lock serializes build CREATION but releases at the
# resolution commit; these give EXECUTION mutual-exclusion so two dispatches of
# one job don't both run the pipeline. Two invariants make it crash-safe: expiry
# is always the DB clock (single source — never the caller's wall time, which
# would let clock skew between workers steal a live lease), and every decision
# lives in the WHERE of the write, never a prior unlocked read — so concurrent
# claims on a free/expired lease resolve to exactly one winner (the class-10
# TOCTOU lesson, the same shape as request_cancel's status-guarded update).


def _lease_expiry(ttl_seconds: float) -> sa.ColumnElement[Any]:
    """``now() + ttl`` computed in Postgres, so every worker measures the lease
    against one clock regardless of its own."""
    return sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)


async def acquire_lease(
    conn: AsyncConnection, job_id: uuid.UUID, owner: str, ttl_seconds: float
) -> bool:
    """Atomically claim the execution lease for ``owner``. Succeeds iff the lease
    is free (no owner) or expired (a crashed holder's ``lease_expires_at`` is now
    past) — one conditional UPDATE, so two workers racing a free/expired lease
    resolve to a single winner. Returns True if this call now holds the lease."""
    won = (
        await conn.execute(
            tables.jobs.update()
            .where(
                tables.jobs.c.id == job_id,
                sa.or_(
                    tables.jobs.c.lease_owner.is_(None),
                    tables.jobs.c.lease_expires_at < sa.func.now(),
                ),
            )
            .values(lease_owner=owner, lease_expires_at=_lease_expiry(ttl_seconds))
            .returning(tables.jobs.c.id)
        )
    ).one_or_none()
    return won is not None


async def renew_lease(
    conn: AsyncConnection, job_id: uuid.UUID, owner: str, ttl_seconds: float
) -> bool:
    """Push the lease's expiry out by ``ttl_seconds`` — the heartbeat. Guarded on
    ``lease_owner == owner``, so a worker that already lost its lease to a reclaim
    cannot extend the new holder's. Returns True if still ours and renewed."""
    won = (
        await conn.execute(
            tables.jobs.update()
            .where(tables.jobs.c.id == job_id, tables.jobs.c.lease_owner == owner)
            .values(lease_expires_at=_lease_expiry(ttl_seconds))
            .returning(tables.jobs.c.id)
        )
    ).one_or_none()
    return won is not None


async def release_lease(conn: AsyncConnection, job_id: uuid.UUID, owner: str) -> None:
    """Clear the lease iff ``owner`` still holds it — a no-op if a reclaim already
    reassigned it (never steal another worker's lease). Both fields drop together
    (the jobs_lease_paired invariant)."""
    await conn.execute(
        tables.jobs.update()
        .where(tables.jobs.c.id == job_id, tables.jobs.c.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None)
    )


async def find_reapable_jobs(conn: AsyncConnection) -> list[tuple[uuid.UUID, str, datetime]]:
    """Jobs whose execution lease has EXPIRED while the job is still non-terminal —
    a worker acquired the lease then stopped heartbeating (crashed / event-loop
    starved), so its dispatch is stuck mid-flight. Returns
    ``(id, project, lease_expires_at)`` for each; the BA2d-3 reaper re-enqueues
    them for a fresh dispatch that reclaims the now-free lease and resumes. The
    stale expiry doubles as the recovery GENERATION marker: it stays byte-stable
    while the row sits crashed (only an acquire/renew moves it), so the reaper
    derives its arq dedup id from it — one pending recovery per stale lease, not
    one per tick. Nothing else matches: a LIVE worker keeps ``lease_expires_at``
    in the future; a completed run released the lease (``lease_owner`` NULL); a
    never-dispatched job never acquired one. Because the worker enters the lease
    FIRST (before its preflight), any crash mid-dispatch — even before run_build —
    leaves a held lease this predicate sees. The job's own status gates on
    non-terminal (``queued``/``running``) so a build that finished but crashed
    before the lease release isn't pointlessly re-run."""
    rows = (
        await conn.execute(
            sa.select(
                tables.jobs.c.id, tables.jobs.c.project, tables.jobs.c.lease_expires_at
            ).where(
                tables.jobs.c.lease_owner.is_not(None),
                tables.jobs.c.lease_expires_at < sa.func.now(),
                tables.jobs.c.status.in_(_ACTIVE_STATUSES),
            )
        )
    ).all()
    return [(row.id, row.project, row.lease_expires_at) for row in rows]


async def capture_config_snapshot(
    conn: AsyncConnection, job_id: uuid.UUID, live_config: dict[str, Any]
) -> dict[str, Any]:
    """Return the config pinned to this job, defensively pinning ``live_config`` if
    it has none yet. ``create_job`` pins ``config_snapshot`` at job creation, so the
    normal path here is a READ: every dispatch (an arq retry, or the BA2d-3 reaper
    re-enqueuing a crashed build) reads that same value back, so a mid-build
    ``PATCH /projects`` can't drift a resuming build's chunking/ontology params
    (which would break convergent idempotency or mix outputs). The write is the
    fallback for a job that somehow lacks a snapshot: one atomic COALESCE UPDATE
    pins it on the first dispatch, and two dispatches racing that pin converge on a
    single stored config. Falls back to ``live_config`` only if the job row is gone
    (run_build then raises JobNotFoundError)."""
    row = (
        await conn.execute(
            tables.jobs.update()
            .where(tables.jobs.c.id == job_id)
            .values(
                config_snapshot=sa.func.coalesce(
                    tables.jobs.c.config_snapshot, sa.literal(live_config, postgresql.JSONB)
                )
            )
            .returning(tables.jobs.c.config_snapshot)
        )
    ).one_or_none()
    return row.config_snapshot if row is not None else live_config


async def count_active_jobs(conn: AsyncConnection, project: str) -> int:
    """Number of still-running (queued/running) jobs for a project — the
    delete_project guard reads this to refuse deletion mid-operation."""
    return int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(tables.jobs)
                .where(
                    tables.jobs.c.project == project,
                    tables.jobs.c.status.in_(_ACTIVE_STATUSES),
                )
            )
        ).scalar_one()
    )
