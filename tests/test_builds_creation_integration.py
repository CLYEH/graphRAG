"""Why: registry-aware build creation (BA2c) is the gate the BA2b FK exists for
— a build must not come into being without its project. These prove, against
live Postgres, that create_build mints a `building` build for a real project,
raises the clean typed error for an absent one (so the router gets a 404, not a
raw FK violation), and that the FK backstops a project that vanishes between the
check and the insert. create_build does NOT commit, so all work runs in a
rolled-back transaction and nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.creation import create_build
from core.config import get_settings
from core.registry import create_project
from core.registry.store import ProjectNotFoundError
from core.stores.tables import builds

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def test_create_build_mints_a_building_build(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            await create_project(conn, name=project)

            build_id = await create_build(conn, project, config_hash="cfg", source_hash="src")

            row = (
                await conn.execute(
                    sa.select(
                        builds.c.project,
                        builds.c.status,
                        builds.c.config_hash,
                        builds.c.source_hash,
                        builds.c.started_at,
                        builds.c.finished_at,
                    ).where(builds.c.id == build_id)
                )
            ).one()
            assert row.project == project
            assert row.status == "building"  # the only state a fresh build starts in
            assert row.config_hash == "cfg"
            assert row.source_hash == "src"
            assert row.started_at is not None  # stamped by now() at creation
            assert row.finished_at is None  # not terminal yet
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_create_build_rejects_an_unknown_project(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(ProjectNotFoundError):
                await create_build(conn, f"missing-{uuid.uuid4().hex[:8]}")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_create_build_fk_backstops_a_racing_project_delete(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit get_project check can pass and the project still vanish
    before the insert commits — the FK (RESTRICT) rejects the insert, which
    create_build maps to the same clean ProjectNotFoundError. Simulate the race
    by making get_project report a project that isn't really there, so the
    insert hits the FK (SQLSTATE 23503)."""
    engine = _engine()

    async def _pretend_present(conn: AsyncConnection, name: str) -> object:
        return object()  # truthy → create_build proceeds to the insert

    monkeypatch.setattr("core.builds.creation.get_project", _pretend_present)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(ProjectNotFoundError):
                await create_build(conn, f"never-existed-{uuid.uuid4().hex[:8]}")
            await trans.rollback()
    finally:
        await engine.dispose()
