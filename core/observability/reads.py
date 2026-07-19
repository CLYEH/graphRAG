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

from core.observability.spec import ItemOutcome
from core.stores import tables

#: The §5 pipeline step whose per-document failures drive RB1-retry-skip's
#: re-extraction. Verbatim from ``orchestrator._STAGE_ORDER`` (a test pins the
#: lockstep) — the LLM-cost stage, the only one whose failed docs must be
#: re-extracted rather than reused.
GRAPH_STEP_NAME = "graph"

#: The §5 stage right AFTER ``graph`` — the one that MERGES entities. Its presence
#: in the parent's latest run means the graph layer is POST-resolve (see
#: :func:`latest_run_ran_resolve`), which RB1-retry-skip's selective clone can't
#: faithfully reuse. Verbatim from ``orchestrator._STAGE_ORDER`` (lockstep-tested).
RESOLVE_STEP_NAME = "resolve"

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


async def latest_run_graph_items(
    conn: AsyncConnection, project: str, build_id: uuid.UUID
) -> list[ItemOutcome]:
    """The ``graph`` step's recorded item outcomes for the build's LATEST run.

    RB1-retry-skip's failed-set source. The caller runs these through
    :func:`core.observability.spec.retry_failed_only` (this is its production
    caller) to get the failed ``(item_kind, item_ref)`` set the child re-extracts;
    everything else (``extracted``/``skipped``) is reused via the graph-layer clone.

    Scoped to the GRAPH step specifically, not all steps: a document can be
    ``failed`` at ``index`` (a chunk-embed failure) while ``extracted`` at
    ``graph`` — its graph artifacts are good and must NOT be re-extracted, so only
    graph-step failures drive re-extraction. Scoped to the LATEST run (§27.7 "前次
    run 的 failed 集合"): a parent that was itself resumed/retried holds several
    runs, and an older run's failures are stale.

    Returns ``[]`` when the parent has no graph step in its latest run (it failed
    at/before ``clean``) — the caller then re-extracts nothing and clones the whole
    (pre-graph, empty) graph layer, i.e. a full fresh graph build.
    """
    runs = tables.pipeline_runs
    steps = tables.pipeline_steps
    items = tables.pipeline_step_items
    latest_run = (
        sa.select(runs.c.id)
        .where(runs.c.project == project, runs.c.build_id == build_id)
        .order_by(sa.func.coalesce(runs.c.started_at, _EPOCH).desc(), runs.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        await conn.execute(
            sa.select(items.c.item_kind, items.c.item_ref, items.c.status)
            .select_from(items.join(steps, items.c.step_id == steps.c.id))
            .where(steps.c.run_id == latest_run, steps.c.step_name == GRAPH_STEP_NAME)
        )
    ).all()
    return [ItemOutcome(item_kind=r.item_kind, item_ref=r.item_ref, status=r.status) for r in rows]


async def latest_run_ran_resolve(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> bool:
    """Whether the build's LATEST run progressed into ``resolve`` (the stage right
    after ``graph``).

    RB1-retry-skip's selective clone assumes a PRE-resolve graph layer. But §22
    TOLERATES under-threshold graph-item failures, so a parent can fail a document
    at ``graph`` (a non-empty failed set), run ``resolve`` anyway, and only then
    fail at ``index``/``summarize``. Resolve MERGES entities — the loser is left as
    a ``status='merged'`` audit row with its mentions REPOINTED to the survivor,
    and merged relations are demoted to NULL signatures (§17/§27.3,
    ``core/resolve/resolution.py``). The clone's success-mention predicate then
    omits those loser rows, and ``extract_only`` skips the (successful) document
    that produced them, so the child would silently LOSE merged audit rows and
    diverge from a full re-derive. When resolve ran, the caller must fall back to a
    full extraction (retry-core's behavior), never the clone+skip (Codex #103 /
    v1 fork C — the clone+skip applies only when the parent stopped AT graph).
    """
    runs = tables.pipeline_runs
    steps = tables.pipeline_steps
    latest_run = (
        sa.select(runs.c.id)
        .where(runs.c.project == project, runs.c.build_id == build_id)
        .order_by(sa.func.coalesce(runs.c.started_at, _EPOCH).desc(), runs.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        await conn.execute(
            sa.select(steps.c.id)
            .where(steps.c.run_id == latest_run, steps.c.step_name == RESOLVE_STEP_NAME)
            .limit(1)
        )
    ).first() is not None


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
    """One page of a step's recorded item outcomes (id desc keyset). Scoped by
    step_id AND the step belonging to ``(project, build_id)`` — this seam is
    public, so it must enforce its own signature rather than trust a caller's
    prior :func:`step_belongs_to_build` precheck (Codex #99 R3: a direct or
    future retry caller passing a FOREIGN step_id must get nothing, not that
    step's items). ``status`` narrows to e.g. failed items."""
    items = tables.pipeline_step_items
    where: list[Any] = [
        items.c.step_id == step_id,
        items.c.step_id.in_(_build_step_ids(project, build_id)),  # the step must be the build's
    ]
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
