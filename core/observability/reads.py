"""Read the §27.7 observability tables for the Console drill-down (RB1).

The pipeline_runs → pipeline_steps → pipeline_step_items chain records HOW a
build ran, step by step and (for failed/skipped items at default verbosity)
item by item. These are the diagnosis surface behind "retry failed only": a
curator drills into a failed build to see which step failed and which items.

These tables are NOT DR-006 build-scoped (they hang off pipeline_runs, which is
the control-plane run record, not the build projection), so the build-scoped
repo layer rejects them — reads go through the raw connection here. Scoping is
by the run's ``(project, build_id)``: a step belongs to a build iff its run
does, and an item belongs to a build iff its step does. Keyset pagination is
opaque-cursor (BA3): steps page NEWEST RUN FIRST — ``(run started_at desc,
step id desc)`` — items id desc (one step is one run, no cross-run order).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables

#: sentinel for a run with no ``started_at`` (would be NULL) — sorts as the
#: OLDEST run so NULLs land last in the newest-run-first order without NULLS-
#: LAST comparison special-casing in the keyset.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_STEP_COLS = (
    tables.pipeline_steps.c.id,
    tables.pipeline_steps.c.step_name,
    tables.pipeline_steps.c.status,
    tables.pipeline_steps.c.started_at,
    tables.pipeline_steps.c.finished_at,
    tables.pipeline_steps.c.input_count,
    tables.pipeline_steps.c.output_count,
    tables.pipeline_steps.c.skipped_count,
    tables.pipeline_steps.c.failed_count,
    tables.pipeline_steps.c.error,
)

_ITEM_COLS = (
    tables.pipeline_step_items.c.id,
    tables.pipeline_step_items.c.item_kind,
    tables.pipeline_step_items.c.item_ref,
    tables.pipeline_step_items.c.status,
    tables.pipeline_step_items.c.message,
    tables.pipeline_step_items.c.error,
)


def _build_step_ids(project: str, build_id: uuid.UUID) -> sa.Select[Any]:
    """The step ids belonging to ``(project, build_id)`` — a step is the build's
    iff its run is. Backs :func:`step_belongs_to_build` (the item drill-down's
    existence precheck); :func:`list_build_steps` inlines the same join to also
    order by the run's timestamp."""
    runs = tables.pipeline_runs
    steps = tables.pipeline_steps
    return (
        sa.select(steps.c.id)
        .select_from(steps.join(runs, steps.c.run_id == runs.c.id))
        .where(runs.c.project == project, runs.c.build_id == build_id)
    )


async def list_build_steps(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
    status: str | None = None,
) -> tuple[Sequence[sa.Row[Any]], tuple[datetime, uuid.UUID] | None]:
    """One page of a build's pipeline steps, NEWEST RUN FIRST (the frozen
    contract's order). A build can hold MORE than one run (a retry/resume, RB1's
    whole point), so ordering by the random ``pipeline_steps.id`` would interleave
    runs and surface a stale run's steps before the latest failure. Order by the
    run's ``started_at`` desc (NULL → epoch sentinel, sorts oldest) with
    ``step.id`` desc as the stable tie-break; the keyset carries both.
    ``status`` narrows to one step status (open vocabulary — blankness only)."""
    runs = tables.pipeline_runs
    steps = tables.pipeline_steps
    run_started = sa.func.coalesce(runs.c.started_at, _EPOCH).label("run_started_at")
    query = (
        sa.select(*_STEP_COLS, run_started)
        .select_from(steps.join(runs, steps.c.run_id == runs.c.id))
        .where(runs.c.project == project, runs.c.build_id == build_id)
    )
    if status is not None:
        query = query.where(steps.c.status == status)
    if after is not None:
        after_ts, after_id = after
        # DESC keyset: the next page is everything strictly "less" than the
        # cursor in (run_started desc, step.id desc) — a row-value comparison,
        # both components non-null (coalesced), so no NULL special-casing.
        query = query.where(
            sa.tuple_(sa.func.coalesce(runs.c.started_at, _EPOCH), steps.c.id)
            < sa.tuple_(sa.literal(after_ts), sa.literal(after_id))
        )
    rows = (
        await conn.execute(query.order_by(run_started.desc(), steps.c.id.desc()).limit(limit + 1))
    ).all()
    page = rows[:limit]
    next_after = (page[-1].run_started_at, page[-1].id) if len(rows) > limit and page else None
    return page, next_after


async def step_belongs_to_build(
    conn: AsyncConnection, project: str, build_id: uuid.UUID, step_id: uuid.UUID
) -> bool:
    """Whether ``step_id`` is a step of ``(project, build_id)`` — the item
    drill-down's existence precheck (a step of another build/project is a 404,
    not an empty page that reads as "this step has no items")."""
    return (
        await conn.execute(
            _build_step_ids(project, build_id).where(tables.pipeline_steps.c.id == step_id).limit(1)
        )
    ).first() is not None


async def list_step_items(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    step_id: uuid.UUID,
    *,
    limit: int,
    after: uuid.UUID | None = None,
    status: str | None = None,
) -> tuple[Sequence[sa.Row[Any]], uuid.UUID | None]:
    """One page of a step's recorded item outcomes (id desc keyset). The step is
    already known to belong to the build (:func:`step_belongs_to_build`); scope
    by step_id. ``status`` narrows to e.g. failed items."""
    items = tables.pipeline_step_items
    where: list[Any] = [items.c.step_id == step_id]
    if status is not None:
        where.append(items.c.status == status)
    if after is not None:
        where.append(items.c.id < after)
    rows = (
        await conn.execute(
            sa.select(*_ITEM_COLS).where(*where).order_by(items.c.id.desc()).limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    next_after = page[-1].id if len(rows) > limit and page else None
    return page, next_after
