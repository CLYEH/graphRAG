"""Why: the partial unique index IS the mechanical DR-001 guarantee. Verify on
real Postgres that a second `active` build for the same project is impossible
at the database level — while co-existing non-active builds and other
projects' active builds stay unaffected (§14: multiple builds per project,
at most one active).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.tables import builds, projects
from tests.conftest import ensure_project

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


async def test_second_active_build_is_impossible(migrated: None) -> None:
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await ensure_project(conn, project)
            await conn.execute(builds.insert().values(project=project, status="active"))
            with pytest.raises(IntegrityError, match="one_active_build"):
                await conn.execute(builds.insert().values(project=project, status="active"))
            await trans.rollback()  # no residue in the dev database
    finally:
        await engine.dispose()


async def test_only_the_active_status_is_constrained(migrated: None) -> None:
    engine = _engine()
    p1 = f"itest-{uuid.uuid4().hex[:10]}"
    p2 = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # many builds per project, one active; other projects independent
            await ensure_project(conn, p1)
            for status in ("building", "ready", "failed", "archived", "active"):
                await conn.execute(builds.insert().values(project=p1, status=status))
            await ensure_project(conn, p2)
            await conn.execute(builds.insert().values(project=p2, status="active"))
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_status_outside_the_lifecycle_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await ensure_project(conn, "itest-x")
            with pytest.raises(IntegrityError, match="builds_status_valid"):
                await conn.execute(builds.insert().values(project="itest-x", status="deployed"))
            await trans.rollback()
    finally:
        await engine.dispose()


def test_upgrade_0010_reconciles_orphan_builds_before_the_fk(require_services: None) -> None:
    """0010 must apply on a DB that already has builds inserted after 0007's
    backfill but before this FK existed (no `projects` row) — the migration
    re-runs the builds→projects backfill first, so ADD CONSTRAINT can't fail on
    an orphan and block the upgrade. Sync test: alembic's env.py drives its own
    asyncio.run, which can't run inside an async test's loop, so the DB work goes
    through asyncio.run() at the top level."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    orphan = f"itest-orphan-{uuid.uuid4().hex[:8]}"

    async def _insert_orphan_build() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                # allowed at 0009 (no FK yet); commit so the migration sees it
                await conn.execute(builds.insert().values(project=orphan, status="ready"))
                await conn.commit()
        finally:
            await engine.dispose()

    async def _assert_reconciled() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                registered = (
                    await conn.execute(sa.select(projects.c.name).where(projects.c.name == orphan))
                ).one_or_none()
                assert registered is not None  # backfill registered the orphan's project
                survived = (
                    await conn.execute(sa.select(builds.c.id).where(builds.c.project == orphan))
                ).one_or_none()
                assert survived is not None  # the build wasn't dropped
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = _engine()
        try:
            async with engine.connect() as conn:
                # build first (FK RESTRICT once 0010 is applied), then its project
                await conn.execute(builds.delete().where(builds.c.project == orphan))
                await conn.execute(projects.delete().where(projects.c.name == orphan))
                await conn.commit()
        finally:
            await engine.dispose()

    try:
        command.downgrade(cfg, "0009_jobs")  # drop the FK + index
        asyncio.run(_insert_orphan_build())
        command.upgrade(cfg, "head")  # MUST NOT fail on the orphan
        asyncio.run(_assert_reconciled())
    finally:
        # robust to either migration state: remove the orphan, then ensure head
        asyncio.run(_cleanup())
        command.upgrade(cfg, "head")
