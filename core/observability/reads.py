"""Read the §27.7 observability tables for the Console drill-down (RB1).

The pipeline_runs → pipeline_steps → pipeline_step_items chain records HOW a
build ran, step by step and (for failed/skipped items at default verbosity)
item by item. These are the diagnosis surface behind "retry failed only": a
curator drills into a failed build to see which step failed and which items.

These tables are NOT DR-006 build-scoped (they hang off pipeline_runs, which is
the control-plane run record, not the build projection), so the build-scoped
repo layer rejects them — reads go through the raw connection here. Scoping is
by the run's ``(project, build_id)``: a step belongs to a build iff its run
does, and an item belongs to a build iff its step does. Keyset pagination
mirrors BA3 (id desc, opaque cursor).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables

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
    iff its run is. Used both to scope the item reads and to answer "does this
    step belong to this build" without a second join spelled out each time."""
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
    after: uuid.UUID | None = None,
    status: str | None = None,
) -> tuple[Sequence[sa.Row[Any]], uuid.UUID | None]:
    """One page of a build's pipeline steps (id desc keyset). ``status`` narrows
    to one step status (open vocabulary — the caller validates blankness)."""
    steps = tables.pipeline_steps
    where: list[Any] = [steps.c.id.in_(_build_step_ids(project, build_id))]
    if status is not None:
        where.append(steps.c.status == status)
    if after is not None:
        where.append(steps.c.id < after)
    rows = (
        await conn.execute(
            sa.select(*_STEP_COLS).where(*where).order_by(steps.c.id.desc()).limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    next_after = page[-1].id if len(rows) > limit and page else None
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
