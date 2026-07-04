"""Why: DR-006 is only real if a repo bound via for_active_build actually
reads the active build's rows AND NOTHING ELSE on live Postgres — two builds
coexisting in the same tables is the §14 normal state, not an edge case. The
unit tests pin the SQL shape; these prove the isolation and the DR-001 lookup
against the real partial-index-guarded builds table.
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

from core.config import get_settings
from core.stores.repo import (
    BuildNotWritableError,
    BuildScopedRepo,
    BuildScopedWriter,
    NoActiveBuildError,
    active_build_id,
)
from core.stores.tables import builds, documents

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


async def _insert_build(conn: AsyncConnection, project: str, status: str) -> uuid.UUID:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status=status).returning(builds.c.id)
        )
    ).scalar_one()
    return build_id


async def _insert_document(conn: AsyncConnection, project: str, build: uuid.UUID) -> None:
    await conn.execute(
        documents.insert().values(
            project=project,
            build_id=build,
            source_uri=f"s3://bucket/{build}",
            content_hash=f"c-{build}",
        )
    )


async def test_active_repo_reads_only_the_active_build(migrated: None) -> None:
    """Two builds' rows coexist in the same table (§14); the repo bound via
    for_active_build must return the active build's rows and nothing else —
    the exact "mixed old-version data" DR-006 exists to make impossible."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, project, "active")
            stale = await _insert_build(conn, project, "ready")
            await _insert_document(conn, project, active)
            await _insert_document(conn, project, stale)

            repo = await BuildScopedRepo.for_active_build(conn, project)
            assert repo.build_id == active
            rows = await repo.fetch_all(documents)
            assert [row.build_id for row in rows] == [active]
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_no_active_build_raises_the_typed_error(migrated: None) -> None:
    """§15's NO_ACTIVE_BUILD is a defined condition, not a crash — core must
    surface it as the typed error the API layer maps onto the frozen code."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await _insert_build(conn, project, "ready")  # exists, but not active
            with pytest.raises(NoActiveBuildError) as excinfo:
                await active_build_id(conn, project)
            assert excinfo.value.project == project
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_pipeline_writes_land_in_the_bound_building_build(migrated: None) -> None:
    """§27.1: 寫入一律指定 building 的 build_id — a repo bound to the building
    build writes there, and the active-bound repo doesn't see those rows."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, project, "active")
            building = await _insert_build(conn, project, "building")

            writer = await BuildScopedWriter.for_building_build(conn, project, building)
            await writer.insert(documents, source_uri="s3://new", content_hash="c-new")

            written = await writer.fetch_all(documents)
            assert [row.build_id for row in written] == [building]
            reader = await BuildScopedRepo.for_active_build(conn, project)
            assert reader.build_id == active
            assert await reader.fetch_all(documents) == []
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_scoped_select_composes_with_caller_predicates(migrated: None) -> None:
    """Callers layer their own filters ON TOP of the scope — adding a WHERE
    must never widen the read back across builds."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, project, "active")
            stale = await _insert_build(conn, project, "ready")
            await _insert_document(conn, project, active)
            await _insert_document(conn, project, stale)

            repo = await BuildScopedRepo.for_active_build(conn, project)
            # the stale build's uri exists in the table, but outside the scope
            rows = await repo.fetch_all(documents, documents.c.source_uri == f"s3://bucket/{stale}")
            assert rows == []
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_active_lookup_is_a_single_query_over_the_guarded_index(migrated: None) -> None:
    """DR-001: the lookup trusts the one_active_build partial unique index —
    scalar_one_or_none() would blow up on duplicates, which the database
    already makes impossible (proven in test_builds_migration_integration)."""
    engine = _engine()
    p1 = f"itest-{uuid.uuid4().hex[:10]}"
    p2 = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            b1 = await _insert_build(conn, p1, "active")
            await _insert_build(conn, p2, "active")  # other project's active
            assert await active_build_id(conn, p1) == b1
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_writer_binding_rejects_non_building_targets(migrated: None) -> None:
    """§27.1: 寫入一律指定 building 的 build_id — binding a writer to the
    ACTIVE build (mutating live data), another project's build (crossing
    scopes), or a nonexistent id must fail typed, before any insert runs."""
    engine = _engine()
    p1 = f"itest-{uuid.uuid4().hex[:10]}"
    p2 = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, p1, "active")
            other_building = await _insert_build(conn, p2, "building")

            with pytest.raises(BuildNotWritableError) as excinfo:
                await BuildScopedWriter.for_building_build(conn, p1, active)
            assert excinfo.value.status == "active"
            with pytest.raises(BuildNotWritableError) as cross:
                await BuildScopedWriter.for_building_build(conn, p1, other_building)
            assert cross.value.status is None  # invisible outside its project
            with pytest.raises(BuildNotWritableError) as missing:
                await BuildScopedWriter.for_building_build(conn, p1, uuid.uuid4())
            assert missing.value.status is None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_structural_or_predicates_cannot_widen_the_scope(migrated: None) -> None:
    """The adversarial containment case executed: a two-branch or_ naming
    BOTH builds stays parenthesized (`AND (build_id = :stale OR build_id =
    :active)`), so its stale branch would leak stale rows if scope
    containment ever broke — and only active rows come back. (or_(true(), x)
    would be useless here: SQLAlchemy simplifies it to plain `true`,
    eliminating the adversarial branch entirely.)"""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            active = await _insert_build(conn, project, "active")
            stale = await _insert_build(conn, project, "ready")
            await _insert_document(conn, project, active)
            await _insert_document(conn, project, stale)

            repo = await BuildScopedRepo.for_active_build(conn, project)
            rows = await repo.fetch_all(
                documents,
                sa.or_(documents.c.build_id == stale, documents.c.build_id == active),
            )
            assert [row.build_id for row in rows] == [active]
            await trans.rollback()
    finally:
        await engine.dispose()
