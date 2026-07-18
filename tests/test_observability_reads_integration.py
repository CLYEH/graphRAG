"""Why: the RB1 drill-down reads must scope a build's steps/items through the
pipeline_runs → pipeline_steps → pipeline_step_items chain and NOT leak another
build's or project's rows — the same "never mix versions" guarantee the
build-scoped repo gives, but these tables hang off pipeline_runs (not the
build projection), so the scoping is the join's job. Live SQL proves the join;
the failed-item filter proves the retry-diagnosis surface.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.observability.reads import (
    latest_run_graph_items,
    list_build_steps,
    list_step_items,
    step_belongs_to_build,
)
from core.observability.recorder import StepReport, record_run
from core.observability.spec import ItemOutcome, retry_failed_only
from core.stores import tables
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def project(migrated: None) -> AsyncIterator[str]:
    name = f"obs-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(
            tables.pipeline_runs.delete().where(tables.pipeline_runs.c.project == name)
        )
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == name))
        await conn.commit()
    await engine.dispose()


async def _make_build(conn: AsyncConnection, project: str) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                tables.builds.insert()
                .values(project=project, status="building")
                .returning(tables.builds.c.id)
            )
        ).scalar_one(),
    )


async def test_steps_and_items_scope_to_the_build_and_filter_failed(project: str) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            build_id = await _make_build(conn, project)
            other_build = await _make_build(conn, project)
            await conn.commit()

            # this build's run: one step with a failed + skipped + (filtered) items
            await record_run(
                conn,
                project,
                build_id,
                "build",
                [
                    StepReport(
                        "extract",
                        (
                            ItemOutcome("document", "hash-ok", "indexed"),
                            ItemOutcome("document", "hash-bad", "failed"),
                            ItemOutcome("entity", "key-skip", "skipped"),
                        ),
                    )
                ],
                verbosity="failures",
            )
            # ANOTHER build's run — must never appear in this build's drill-down
            await record_run(
                conn,
                project,
                other_build,
                "build",
                [StepReport("extract", (ItemOutcome("document", "other-bad", "failed"),))],
                verbosity="failures",
            )
            await conn.commit()

            steps, next_after = await list_build_steps(conn, project, build_id, limit=50)
            assert len(steps) == 1 and next_after is None
            (step,) = steps
            assert step.step_name == "extract"
            assert (step.input_count, step.failed_count, step.skipped_count) == (3, 1, 1)

            # the step belongs to THIS build; a random id / another build's step does not
            assert await step_belongs_to_build(conn, project, build_id, step.id) is True
            assert await step_belongs_to_build(conn, project, build_id, uuid.uuid4()) is False
            assert await step_belongs_to_build(conn, project, other_build, step.id) is False

            # default verbosity records only failed/skipped — both here
            items, _ = await list_step_items(conn, project, build_id, step.id, limit=50)
            assert {(i.item_ref, i.status) for i in items} == {
                ("hash-bad", "failed"),
                ("key-skip", "skipped"),
            }
            # the retry-diagnosis facet: filter[status]=failed narrows to the retry set
            failed, _ = await list_step_items(
                conn, project, build_id, step.id, limit=50, status="failed"
            )
            assert {(i.item_ref, i.status) for i in failed} == {("hash-bad", "failed")}

            # Codex #99 R3: the seam SELF-scopes — asking for THIS step's items
            # under the WRONG build returns nothing (a foreign-build caller can't
            # leak them), independent of any router precheck
            leaked, _ = await list_step_items(conn, project, other_build, step.id, limit=50)
            assert leaked == []
            await conn.rollback()
    finally:
        await engine.dispose()


async def _seed_run(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    started_at: datetime,
    steps: dict[str, tuple[tuple[str, str, str], ...]],
) -> None:
    """Seed one pipeline_run at a CONTROLLED ``started_at`` + its named steps +
    their (kind, ref, status) items. ``record_run`` stamps ``started_at=now()``,
    so ordering two runs by time needs this manual insert."""
    run_id = (
        await conn.execute(
            tables.pipeline_runs.insert()
            .values(
                project=project,
                build_id=build_id,
                kind="build",
                status="done",
                started_at=started_at,
            )
            .returning(tables.pipeline_runs.c.id)
        )
    ).scalar_one()
    for step_name, items in steps.items():
        step_id = (
            await conn.execute(
                tables.pipeline_steps.insert()
                .values(run_id=run_id, step_name=step_name, status="done")
                .returning(tables.pipeline_steps.c.id)
            )
        ).scalar_one()
        for kind, ref, status in items:
            await conn.execute(
                tables.pipeline_step_items.insert().values(
                    step_id=step_id, item_kind=kind, item_ref=ref, status=status
                )
            )


async def test_latest_run_graph_items_scopes_to_the_latest_runs_graph_step(project: str) -> None:
    """RB1-retry-skip's failed-set source. Yields ONLY the LATEST run's GRAPH-step
    items: an OLD run's failure is stale (a resumed/retried parent holds several
    runs), and a failure at INDEX — not graph — must NOT drive re-extraction, since
    that document's graph artifacts are already good. ``retry_failed_only`` then
    keeps only the failed DOCUMENT refs — the production wiring this slice adds.

    DISCRIMINATING setup: doc A failed in the OLD run but RECOVERED (extracted) in
    the NEW run; doc C failed at INDEX in the new run. A reader that ignored run
    recency would re-extract A; one that ignored the graph-step scope would
    re-extract C. Only B (the new run's real graph failure) is correct."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            build_id = await _make_build(conn, project)
            await _seed_run(
                conn,
                project,
                build_id,
                datetime(2026, 1, 1, tzinfo=UTC),
                {"graph": (("document", "A", "failed"),)},
            )
            await _seed_run(
                conn,
                project,
                build_id,
                datetime(2026, 6, 1, tzinfo=UTC),
                {
                    "graph": (("document", "A", "extracted"), ("document", "B", "failed")),
                    "index": (("document", "C", "failed"),),
                },
            )
            await conn.commit()

            items = await latest_run_graph_items(conn, project, build_id)
            assert {(i.item_ref, i.status) for i in items} == {("A", "extracted"), ("B", "failed")}
            failed = {ref for (kind, ref) in retry_failed_only(items) if kind == "document"}
            assert failed == {"B"}  # not A (recovered), not C (index-only failure)
            await conn.rollback()
    finally:
        await engine.dispose()


async def _make_run_with_step(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    started_at: datetime,
    step_name: str,
    step_id: uuid.UUID,
) -> None:
    run_id = (
        await conn.execute(
            tables.pipeline_runs.insert()
            .values(
                project=project,
                build_id=build_id,
                kind="build",
                status="done",
                started_at=started_at,
            )
            .returning(tables.pipeline_runs.c.id)
        )
    ).scalar_one()
    await conn.execute(
        tables.pipeline_steps.insert().values(
            id=step_id, run_id=run_id, step_name=step_name, status="done"
        )
    )


async def test_steps_order_newest_run_first_across_runs(project: str) -> None:
    """Codex #99 R1: a build with MORE than one run (a retry/resume) must list
    the NEWEST run's steps first — the frozen contract order. Ordering by the
    random pipeline_steps.id would interleave runs; the run's started_at drives
    it. DISCRIMINATING setup: give the OLD run's step a HIGHER uuid than the NEW
    run's, so a (buggy) id-desc order would put OLD first — the assertion below
    (NEW first) then can ONLY pass via the run-timestamp order."""
    engine = _engine()
    hi = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")  # old run's step
    lo = uuid.UUID("00000000-0000-4000-8000-000000000000")  # new run's step
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            build_id = await _make_build(conn, project)
            await _make_run_with_step(
                conn, project, build_id, datetime(2026, 1, 1, tzinfo=UTC), "old-run-step", hi
            )
            await _make_run_with_step(
                conn, project, build_id, datetime(2026, 6, 1, tzinfo=UTC), "new-run-step", lo
            )
            await conn.commit()

            steps, _ = await list_build_steps(conn, project, build_id, limit=50)
            assert [s.step_name for s in steps] == ["new-run-step", "old-run-step"]

            # the keyset round-trips: page size 1 returns the newest, and its
            # cursor fetches the older next (never re-returns the newest)
            first, cursor = await list_build_steps(conn, project, build_id, limit=1)
            assert [s.step_name for s in first] == ["new-run-step"] and cursor is not None
            second, cursor2 = await list_build_steps(conn, project, build_id, limit=1, after=cursor)
            assert [s.step_name for s in second] == ["old-run-step"] and cursor2 is None
            await conn.rollback()
    finally:
        await engine.dispose()
