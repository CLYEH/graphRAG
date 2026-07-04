"""Why: DR-004 puts every build's projection in ONE Neo4j database, so "the
reader sees only the active build" is only real if two builds' graphs
actually coexist and the scoped templates separate them on a live server.
These tests also prove the cross-store §27.1 write guarantee: the Postgres
row lock — the one place activation and projection meet — really makes them
mutually exclusive.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from neo4j import AsyncSession
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.graph import (
    BuildScopedGraphProjector,
    BuildScopedGraphRepo,
    RelationEndpointsNotProjectedError,
    graph_driver,
)
from core.stores.repo import BuildNotWritableError, NoActiveBuildError
from core.stores.tables import builds

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_WIPE_PROJECT = """\
MATCH (n:Entity {project: $project})
DETACH DELETE n
"""


@pytest.fixture()
def migrated(require_services: None) -> None:
    """Postgres migrations (idempotent); Neo4j needs no schema — DR-004 is
    property-filtered, not database-per-build."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def graph_session(migrated: None) -> AsyncIterator[AsyncSession]:
    driver = graph_driver()
    async with driver.session() as session:
        yield session
    await driver.close()


async def _insert_build(conn: AsyncConnection, project: str, status: str) -> uuid.UUID:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status=status).returning(builds.c.id)
        )
    ).scalar_one()
    return build_id


async def _wipe(session: AsyncSession, project: str) -> None:
    await (await session.run(_WIPE_PROJECT, {"project": project})).consume()


async def test_reader_sees_only_the_active_builds_projection(
    graph_session: AsyncSession,
) -> None:
    """DR-004's normal state: two builds' nodes coexist in the single
    database. The active-bound reader must return the active build's
    entities and counts and NOTHING else — the exact version mixing DR-006
    exists to make impossible."""
    engine = _engine()
    project = f"gtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            old = await _insert_build(conn, project, "building")
            new = await _insert_build(conn, project, "building")
            await conn.commit()

            for build, marker in ((old, "old"), (new, "new")):
                projector = await BuildScopedGraphProjector.for_building_build(
                    conn, graph_session, project, build
                )
                await projector.project_entity(f"e-{marker}", "person", "resolved", marker)
                await conn.commit()  # release the FOR SHARE before flipping status

            await conn.execute(builds.update().where(builds.c.id == new).values(status="active"))
            await conn.execute(builds.update().where(builds.c.id == old).values(status="archived"))
            await conn.commit()

            reader = await BuildScopedGraphRepo.for_active_build(conn, graph_session, project)
            assert reader.build_id == new
            entities = await reader.fetch_entities()
            assert [e["canonical_id"] for e in entities] == ["e-new"]
            assert {e["build_id"] for e in entities} == {str(new)}
            assert await reader.entity_count() == 1
    finally:
        await _wipe(graph_session, project)
        async with engine.connect() as cleanup:
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.commit()
        await engine.dispose()


async def test_relations_project_and_count_within_the_scope(
    graph_session: AsyncSession,
) -> None:
    """§4: edges are [:REL {build_id, type}] between same-build endpoints;
    the scoped relation count sees them, and endpoints missing FROM THIS
    BUILD refuse loudly instead of silently projecting nothing."""
    engine = _engine()
    project = f"gtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            building = await _insert_build(conn, project, "building")
            await conn.commit()

            projector = await BuildScopedGraphProjector.for_building_build(
                conn, graph_session, project, building
            )
            await projector.project_entity("e-a", "person", "resolved", "A")
            await projector.project_entity("e-b", "org", "resolved", "B")
            await projector.project_relation("e-a", "e-b", "works_at")
            assert await projector.relation_count() == 1
            # idempotent re-projection (§5 retries) must not duplicate
            await projector.project_relation("e-a", "e-b", "works_at")
            assert await projector.relation_count() == 1

            with pytest.raises(RelationEndpointsNotProjectedError) as excinfo:
                await projector.project_relation("e-a", "e-ghost", "works_at")
            assert excinfo.value.dst == "e-ghost"
            await conn.commit()
    finally:
        await _wipe(graph_session, project)
        async with engine.connect() as cleanup:
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.commit()
        await engine.dispose()


async def test_factories_validate_against_postgres(graph_session: AsyncSession) -> None:
    """DR-001: Neo4j never decides what is active or writable — both
    factories resolve/validate against the Postgres builds table and raise
    the same typed errors as the Postgres repo."""
    engine = _engine()
    p1 = f"gtest-{uuid.uuid4().hex[:10]}"
    p2 = f"gtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, p1, "active")
            other_building = await _insert_build(conn, p2, "building")

            with pytest.raises(NoActiveBuildError):
                await BuildScopedGraphRepo.for_active_build(conn, graph_session, p2)
            with pytest.raises(BuildNotWritableError) as excinfo:
                await BuildScopedGraphProjector.for_building_build(conn, graph_session, p1, active)
            assert excinfo.value.status == "active"
            with pytest.raises(BuildNotWritableError) as cross:
                await BuildScopedGraphProjector.for_building_build(
                    conn, graph_session, p1, other_building
                )
            assert cross.value.status is None  # invisible outside its project
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_projection_after_activation_is_refused_typed(
    graph_session: AsyncSession,
) -> None:
    """§27.1 across stores: a projector bound while `building` must not keep
    writing after the build activates. The per-write revalidation runs on
    Postgres — the store that owns the status — and surfaces the same typed
    error as the bind-time check."""
    engine = _engine()
    project = f"gtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            building = await _insert_build(conn, project, "building")
            await conn.commit()

            projector = await BuildScopedGraphProjector.for_building_build(
                conn, graph_session, project, building
            )
            await projector.project_entity("e-ok", "person", "resolved")
            await conn.commit()  # release the share lock so activation can run

            async with engine.connect() as other:
                await other.execute(
                    builds.update().where(builds.c.id == building).values(status="active")
                )
                await other.commit()

            with pytest.raises(BuildNotWritableError) as excinfo:
                await projector.project_entity("e-late", "person", "resolved")
            assert excinfo.value.status == "active"
            await conn.rollback()

            # the refused write really landed nowhere: only e-ok exists
            reader = await BuildScopedGraphRepo.for_active_build(conn, graph_session, project)
            assert [e["canonical_id"] for e in await reader.fetch_entities()] == ["e-ok"]
    finally:
        await _wipe(graph_session, project)
        async with engine.connect() as cleanup:
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.commit()
        await engine.dispose()


async def test_inflight_projection_and_activation_are_mutually_exclusive(
    graph_session: AsyncSession,
) -> None:
    """Why FOR SHARE on Postgres and not a plain status recheck: the write
    happens in Neo4j, but activation is a single Postgres transaction (§14)
    that must lock the builds row — which the projector's revalidation holds
    FOR SHARE until its own transaction ends. So activation WAITS for
    in-flight projections; there is no window where both proceed."""
    engine = _engine()
    project = f"gtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            building = await _insert_build(conn, project, "building")
            await conn.commit()

            projector = await BuildScopedGraphProjector.for_building_build(
                conn, graph_session, project, building
            )
            await projector.project_entity("e-w", "person", "resolved")
            # projection txn open on Postgres -> share lock held -> activation blocks
            async with engine.connect() as activator:
                activation = asyncio.ensure_future(
                    activator.execute(
                        builds.update().where(builds.c.id == building).values(status="active")
                    )
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(activation), timeout=1.0)
                await conn.commit()  # projection done; lock released
                await activation
                await activator.commit()

            with pytest.raises(BuildNotWritableError):
                await projector.project_entity("e-late", "person", "resolved")
            await conn.rollback()
    finally:
        await _wipe(graph_session, project)
        async with engine.connect() as cleanup:
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.commit()
        await engine.dispose()
