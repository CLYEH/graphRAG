"""Why: the partial unique index IS the mechanical DR-001 guarantee. Verify on
real Postgres that a second `active` build for the same project is impossible
at the database level — while co-existing non-active builds and other
projects' active builds stay unaffected (§14: multiple builds per project,
at most one active).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.tables import builds

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
            for status in ("building", "ready", "failed", "archived", "active"):
                await conn.execute(builds.insert().values(project=p1, status=status))
            await conn.execute(builds.insert().values(project=p2, status="active"))
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_status_outside_the_lifecycle_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="builds_status_valid"):
                await conn.execute(builds.insert().values(project="itest-x", status="deployed"))
            await trans.rollback()
    finally:
        await engine.dispose()
