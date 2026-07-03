"""Why: the §27.7 invariants (ingest runs carry a build, one outcome row per
item per step) and the cascade-prune behavior are only real if Postgres
enforces them — a CHECK or unique index that exists in metadata but not in the
rendered DDL is writer discipline in disguise. Verified against the real
migrated database, same pattern as the builds/one_active_build tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.tables import pipeline_runs, pipeline_step_items, pipeline_steps

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    """Apply migrations (idempotent). Sync fixture: alembic's env.py drives its
    own asyncio.run, which must not happen inside a running event loop."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _insert_run_step(conn: AsyncConnection, project: str) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = (
        await conn.execute(
            pipeline_runs.insert()
            .values(project=project, kind="build", status="running", build_id=uuid.uuid4())
            .returning(pipeline_runs.c.id)
        )
    ).scalar_one()
    step_id = (
        await conn.execute(
            pipeline_steps.insert()
            .values(run_id=run_id, step_name="graph", status="running")
            .returning(pipeline_steps.c.id)
        )
    ).scalar_one()
    return run_id, step_id


async def test_ingest_run_without_a_build_is_impossible(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="pipeline_runs_ingest_has_build"):
                await conn.execute(
                    pipeline_runs.insert().values(
                        project="itest-x", kind="ingest", status="queued", build_id=None
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_source_validation_style_runs_may_omit_the_build(migrated: None) -> None:
    """The other half of §27.7: a NOT NULL (or an over-wide CHECK) would make
    the pure source-validation job unrepresentable."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await conn.execute(
                pipeline_runs.insert().values(
                    project="itest-x", kind="source_validation", status="queued", build_id=None
                )
            )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_second_outcome_row_for_the_same_item_is_impossible(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            _, step_id = await _insert_run_step(conn, f"itest-{uuid.uuid4().hex[:10]}")
            await conn.execute(
                pipeline_step_items.insert().values(
                    step_id=step_id, item_kind="document", item_ref="hash-a", status="failed"
                )
            )
            with pytest.raises(IntegrityError, match="pipeline_step_items_dedup"):
                await conn.execute(
                    pipeline_step_items.insert().values(
                        step_id=step_id, item_kind="document", item_ref="hash-a", status="skipped"
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_run_status_outside_the_jobs_contract_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="pipeline_runs_status_valid"):
                await conn.execute(
                    pipeline_runs.insert().values(
                        project="itest-x", kind="build", status="succeeded", build_id=uuid.uuid4()
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_deleting_a_run_prunes_its_layers_as_a_unit(migrated: None) -> None:
    """§18 retention: pruning observability must not leave orphaned steps or
    items — the CASCADE chain is what keeps retention a plain DELETE."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            run_id, step_id = await _insert_run_step(conn, f"itest-{uuid.uuid4().hex[:10]}")
            await conn.execute(
                pipeline_step_items.insert().values(
                    step_id=step_id, item_kind="document", item_ref="hash-a", status="failed"
                )
            )
            await conn.execute(pipeline_runs.delete().where(pipeline_runs.c.id == run_id))
            steps_left = (
                await conn.execute(
                    select(func.count())
                    .select_from(pipeline_steps)
                    .where(pipeline_steps.c.run_id == run_id)
                )
            ).scalar_one()
            items_left = (
                await conn.execute(
                    select(func.count())
                    .select_from(pipeline_step_items)
                    .where(pipeline_step_items.c.step_id == step_id)
                )
            ).scalar_one()
            assert (steps_left, items_left) == (0, 0)
            await trans.rollback()
    finally:
        await engine.dispose()
