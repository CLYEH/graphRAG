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
    concurrent delete."""
    row = (
        await conn.execute(
            tables.jobs.insert()
            .values(project=project, kind=kind, build_id=build_id)
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
        await conn.execute(
            tables.jobs.update().where(tables.jobs.c.id == job_id).values(cancel_requested=True)
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
