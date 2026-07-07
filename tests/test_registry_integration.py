"""Why: the registry CRUD is the control plane every later BA-task builds on,
so its behaviors must hold against live Postgres, not just fakes — the JSONB
round-trip, the PATCH null-vs-omitted distinction, keyset pagination on real
`created_at desc, name desc` ordering, and the ON DELETE CASCADE that lets a
project delete rely on the DB to sweep its sources. Fakes can't prove any of
these (they bypass the SQL that enforces them). All work runs in a rolled-back
transaction so nothing lands in the dev DB.
"""

from __future__ import annotations

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
from core.registry import (
    ProjectExistsError,
    ProjectHasBuildsError,
    ProjectNotFoundError,
    add_source,
    create_project,
    delete_project,
    get_project,
    list_projects,
    list_sources,
    update_project,
)
from core.stores.tables import builds, sources

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


async def test_create_get_roundtrip_and_duplicate(migrated: None) -> None:
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            created = await create_project(conn, name=name, display_name="Demo", config={"k": "v"})
            assert created.name == name
            assert created.display_name == "Demo"
            assert created.config == {"k": "v"}  # JSONB round-trips as a dict
            assert created.description is None
            assert created.created_at is not None

            fetched = await get_project(conn, name)
            assert fetched == created  # frozen dataclass equality

            with pytest.raises(ProjectExistsError):
                await create_project(conn, name=name)
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_update_patch_null_vs_omitted(migrated: None) -> None:
    """A passed None clears the column; an omitted field is left untouched —
    the distinction the router's PATCH depends on."""
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name, display_name="Keep", description="Original")
            # omit display_name (untouched), set description to null
            updated = await update_project(conn, name, description=None)
            assert updated is not None
            assert updated.display_name == "Keep"  # omitted → unchanged
            assert updated.description is None  # passed None → cleared

            # empty patch is a no-op read that still returns the row
            noop = await update_project(conn, name)
            assert noop == updated

            # updating a missing project → None (router maps to 404)
            assert await update_project(conn, _proj(), display_name="x") is None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_list_projects_keyset_pagination(migrated: None) -> None:
    engine = _engine()
    names = sorted(_proj() for _ in range(3))
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # force a deterministic created_at order (else same-now() rows tie
            # only on name); insert oldest→newest a second apart
            for i, n in enumerate(names):
                await create_project(conn, name=n)
                await conn.execute(
                    sa.text(
                        "UPDATE projects SET created_at = now() + make_interval(secs => :s) "
                        "WHERE name = :n"
                    ),
                    {"s": i, "n": n},
                )
            page1, after1 = await list_projects(conn, limit=2)
            assert len(page1) == 2
            assert after1 is not None  # a third row remains
            page2, after2 = await list_projects(conn, limit=2, after=after1)
            assert len(page2) == 1
            assert after2 is None  # last page

            seen = [p.name for p in page1 + page2]
            assert set(seen) >= set(names)  # every inserted project surfaced once
            assert len(seen) == len(set(seen))  # no row repeated across pages
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_add_source_requires_project_and_lists(migrated: None) -> None:
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(ProjectNotFoundError):
                await add_source(conn, name, uri="file:///x")  # no such project yet

            await create_project(conn, name=name)
            s = await add_source(conn, name, uri="file:///data", kind="file", metadata={"n": 1})
            assert s.project == name
            assert s.uri == "file:///data"
            assert s.kind == "file"
            assert s.metadata == {"n": 1}

            listed, after = await list_sources(conn, name, limit=10)
            assert [x.id for x in listed] == [s.id]
            assert after is None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_list_sources_keyset_pagination(migrated: None) -> None:
    """The sources keyset (added_at desc, id desc) is distinct from projects'
    and must page live without skips/dupes."""
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            ids = []
            for i in range(3):
                s = await add_source(conn, name, uri=f"file:///{i}")
                await conn.execute(
                    sa.text(
                        "UPDATE sources SET added_at = now() + make_interval(secs => :s) "
                        "WHERE id = :id"
                    ),
                    {"s": i, "id": s.id},
                )
                ids.append(s.id)
            page1, after1 = await list_sources(conn, name, limit=2)
            assert len(page1) == 2
            assert after1 is not None
            page2, after2 = await list_sources(conn, name, limit=2, after=after1)
            assert len(page2) == 1
            assert after2 is None
            seen = [s.id for s in page1 + page2]
            assert set(seen) == set(ids)  # every source once, none repeated
            assert len(seen) == len(set(seen))
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_check_violation_is_not_mislabeled(migrated: None) -> None:
    """An empty name trips the CHECK (23514), not the PK unique (23505) — the
    store must let that IntegrityError through, not mislabel it as 'exists'."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):  # NOT ProjectExistsError
                await create_project(conn, name="")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_delete_project_cascades_sources(migrated: None) -> None:
    """delete_project relies on the FK's ON DELETE CASCADE — after deleting the
    project, its sources must be gone from the table with no app-side sweep."""
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            s = await add_source(conn, name, uri="file:///y")

            assert await delete_project(conn, name) is True
            assert await get_project(conn, name) is None
            remaining = (
                await conn.execute(sa.select(sources.c.id).where(sources.c.id == s.id))
            ).all()
            assert remaining == []  # cascaded, not orphaned

            assert await delete_project(conn, name) is False  # already gone
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_delete_project_refuses_while_builds_exist(migrated: None) -> None:
    """builds.project is bare text (no FK), so deleting a project with builds
    would strand build-scoped data under a reusable name → stale active build
    on recreate. delete_project must refuse until the builds are pruned."""
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            await conn.execute(builds.insert().values(project=name, status="ready"))

            with pytest.raises(ProjectHasBuildsError):
                await delete_project(conn, name)
            assert await get_project(conn, name) is not None  # not deleted

            # once the build is gone, the delete proceeds
            await conn.execute(builds.delete().where(builds.c.project == name))
            assert await delete_project(conn, name) is True
            await trans.rollback()
    finally:
        await engine.dispose()
