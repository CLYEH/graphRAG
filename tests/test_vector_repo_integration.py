"""Why: §4 puts every build's points in ONE per-project collection, so "the
reader sees only the active build" is only real if two builds' points
actually coexist and the payload filter separates them on a live server —
including under kNN, where the nearest neighbor by geometry may belong to
the WRONG build and must still be filtered out. The cross-store §27.1 write
guarantee (Postgres row lock vs activation) is re-proven here for the third
store.
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
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.repo import BuildNotWritableError, NoActiveBuildError
from core.stores.tables import builds
from core.stores.vectors import (
    BuildScopedVectorProjector,
    BuildScopedVectorRepo,
    collection_for,
    vector_client,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
_DIMS = 4


@pytest.fixture()
def migrated(require_services: None) -> None:
    """Postgres migrations (idempotent); Qdrant needs no schema — collections
    are created per project by the projector."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def qdrant(migrated: None) -> AsyncIterator[AsyncQdrantClient]:
    client = vector_client()
    yield client
    await client.close()


async def _insert_build(conn: AsyncConnection, project: str, status: str) -> uuid.UUID:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status=status).returning(builds.c.id)
        )
    ).scalar_one()
    return build_id


async def _cleanup(client: AsyncQdrantClient, engine: AsyncEngine, project: str) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    async with engine.connect() as conn:
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()


async def test_reader_sees_only_the_active_builds_points(
    qdrant: AsyncQdrantClient,
) -> None:
    """Two builds' points coexist in the project's collection; the stale
    build's point is GEOMETRICALLY IDENTICAL to the query vector, so an
    unfiltered kNN would rank it first — the scoped reader must never see
    it, and the drift counts must split by build."""
    engine = _engine()
    project = f"vtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            old = await _insert_build(conn, project, "building")
            new = await _insert_build(conn, project, "building")
            await conn.commit()

            probe = [1.0, 0.0, 0.0, 0.0]
            for build, vector, marker in ((old, probe, "old"), (new, [0.0, 1.0, 0.0, 0.0], "new")):
                projector = await BuildScopedVectorProjector.for_building_build(
                    conn, qdrant, project, build
                )
                await projector.ensure_collection(_DIMS)
                await projector.upsert_point(
                    uuid.uuid4(),
                    vector,
                    canonical_id=f"c-{marker}",
                    point_type="chunk",
                    text=marker,
                    chunk_id=f"c-{marker}",
                )
                await conn.commit()  # release the FOR SHARE before the next bind

            await conn.execute(builds.update().where(builds.c.id == new).values(status="active"))
            await conn.execute(builds.update().where(builds.c.id == old).values(status="archived"))
            await conn.commit()

            reader = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            assert reader.build_id == new
            hits = await reader.search(probe, limit=10)  # nearest overall is the STALE point
            assert [hit.payload["canonical_id"] for hit in hits if hit.payload] == ["c-new"]
            assert await reader.point_count() == 1
    finally:
        await _cleanup(qdrant, engine, project)


async def test_factories_validate_against_postgres(qdrant: AsyncQdrantClient) -> None:
    """DR-001: Qdrant never decides what is active or writable — both
    factories resolve/validate against the Postgres builds table with the
    same typed errors as the sibling repos."""
    engine = _engine()
    p1 = f"vtest-{uuid.uuid4().hex[:10]}"
    p2 = f"vtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, p1, "active")
            other_building = await _insert_build(conn, p2, "building")

            with pytest.raises(NoActiveBuildError):
                await BuildScopedVectorRepo.for_active_build(conn, qdrant, p2)
            with pytest.raises(BuildNotWritableError) as excinfo:
                await BuildScopedVectorProjector.for_building_build(conn, qdrant, p1, active)
            assert excinfo.value.status == "active"
            with pytest.raises(BuildNotWritableError) as cross:
                await BuildScopedVectorProjector.for_building_build(
                    conn, qdrant, p1, other_building
                )
            assert cross.value.status is None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_upsert_after_activation_is_refused_typed(qdrant: AsyncQdrantClient) -> None:
    """§27.1 across stores, third store: a projector bound while `building`
    must not keep writing after the build activates; the refusal is typed
    and the refused point verifiably never landed."""
    engine = _engine()
    project = f"vtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            building = await _insert_build(conn, project, "building")
            await conn.commit()

            projector = await BuildScopedVectorProjector.for_building_build(
                conn, qdrant, project, building
            )
            await projector.ensure_collection(_DIMS)
            await projector.upsert_point(
                uuid.uuid4(), [1.0, 0.0, 0.0, 0.0], canonical_id="ok", point_type="chunk", text="ok"
            )
            await conn.commit()  # release the share lock so activation can run

            async with engine.connect() as other:
                await other.execute(
                    builds.update().where(builds.c.id == building).values(status="active")
                )
                await other.commit()

            with pytest.raises(BuildNotWritableError) as excinfo:
                await projector.upsert_point(
                    uuid.uuid4(),
                    [0.0, 1.0, 0.0, 0.0],
                    canonical_id="late",
                    point_type="chunk",
                    text="late",
                )
            assert excinfo.value.status == "active"
            await conn.rollback()

            reader = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            assert await reader.point_count() == 1  # only "ok" exists
    finally:
        await _cleanup(qdrant, engine, project)


async def test_inflight_upserts_and_activation_are_mutually_exclusive(
    qdrant: AsyncQdrantClient,
) -> None:
    """Same anchor as the Neo4j projector: the FOR SHARE taken by the
    per-write revalidation lives until the projecting Postgres transaction
    ends, and activation's UPDATE needs that row lock — so activation WAITS
    for in-flight upserts instead of racing them."""
    engine = _engine()
    project = f"vtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            building = await _insert_build(conn, project, "building")
            await conn.commit()

            projector = await BuildScopedVectorProjector.for_building_build(
                conn, qdrant, project, building
            )
            await projector.ensure_collection(_DIMS)
            await projector.upsert_point(
                uuid.uuid4(), [1.0, 0.0, 0.0, 0.0], canonical_id="w", point_type="chunk", text="w"
            )
            # projection txn open on Postgres -> share lock held -> activation blocks
            async with engine.connect() as activator:
                activation = asyncio.ensure_future(
                    activator.execute(
                        builds.update().where(builds.c.id == building).values(status="active")
                    )
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(activation), timeout=1.0)
                await conn.commit()
                await activation
                await activator.commit()

            with pytest.raises(BuildNotWritableError):
                await projector.upsert_point(
                    uuid.uuid4(),
                    [0.0, 1.0, 0.0, 0.0],
                    canonical_id="late",
                    point_type="chunk",
                    text="late",
                )
            await conn.rollback()
    finally:
        await _cleanup(qdrant, engine, project)
