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
    list_build_steps,
    list_step_items,
    step_belongs_to_build,
)
from core.observability.recorder import StepReport, record_run
from core.observability.spec import ItemOutcome
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
            await conn.rollback()
    finally:
        await engine.dispose()
