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
    MANAGED_FILES_KEY,
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
    upsert_managed_source,
)
from core.stores.tables import (
    builds,
    ontology_proposals,
    pipeline_runs,
    review_ledger,
    sources,
)
from tests.conftest import ensure_project

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
    """Keyset pagination must not skip or duplicate a row across the page
    boundary. Self-relative: the three projects are pinned to the newest
    created_at so they lead the (created_at desc) order regardless of any rows
    other committing integration tests left in the shared dev DB — the test
    asserts pagination over ITS three, not that the table is otherwise empty."""
    engine = _engine()
    names = sorted(_proj() for _ in range(3))
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # far-future created_at, a second apart, so these three are the newest
            # rows (created_at desc) whatever else is committed; i=2 is newest
            for i, n in enumerate(names):
                await create_project(conn, name=n)
                await conn.execute(
                    sa.text(
                        "UPDATE projects SET created_at = now() + "
                        "make_interval(days => 3650, secs => :s) WHERE name = :n"
                    ),
                    {"s": i, "n": n},
                )
            page1, after1 = await list_projects(conn, limit=2)
            assert after1 is not None  # more rows remain (our 3rd + any leaked)
            page2, _ = await list_projects(conn, limit=2, after=after1)
            # our three lead in created_at-desc order (newest i=2 → i=0); the 3rd
            # must follow contiguously across the limit=2 boundary, no skip/dupe
            ours = [p.name for p in page1] + [page2[0].name]
            assert ours == list(reversed(names))
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


async def test_upsert_managed_source_forces_kind_text_on_a_reused_row(migrated: None) -> None:
    """WHY: the upload endpoint returns 201 pointing at the source this upserts, and
    builds dispatch by ``source.kind`` in ``resolve_source``. If a row already exists
    at the managed corpus uri from ``POST /sources`` with a NON-text kind (structured
    / null / typo), merging only the ``files`` metadata would leave that kind — so the
    accepted managed-text upload routes to the wrong connector (or none) and is never
    ingested. Upsert must (re)assert kind=text on the reused row, not just on insert."""
    engine = _engine()
    name = _proj()
    uri = "file:///managed/corpus"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            # a pre-existing conflicting source at the managed uri (structured)
            stale = await add_source(
                conn, name, uri=uri, kind="structured", metadata={"table": "t"}
            )
            reused = await upsert_managed_source(
                conn, name, uri=uri, kind="text", files={"a.txt": {"context": {"title": "A"}}}
            )
            assert reused.id == stale.id  # same row reused (by project, uri), not a new one
            assert reused.kind == "text"  # kind FORCED to the managed kind
            assert reused.metadata[MANAGED_FILES_KEY] == {"a.txt": {"context": {"title": "A"}}}
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_upsert_managed_source_coalesces_all_duplicate_rows(migrated: None) -> None:
    """WHY: ``sources`` has no ``(project, uri)`` uniqueness, so a project can hold
    MULTIPLE rows at the managed corpus uri, and ``list_sources`` feeds EVERY one to
    the build. Canonicalizing only the oldest would leave a stale duplicate: a fileless
    text row directory-scans the corpus (persisting fallback metadata) and a non-text
    row fails the build. Upsert must coalesce ALL matching rows to the one canonical
    managed-text shape, merging their file stashes — no stale row survives."""
    engine = _engine()
    name = _proj()
    uri = "file:///managed/corpus"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            # two pre-existing duplicate rows at the managed uri: a fileless text row
            # (would directory-scan) and a non-text row (would fail the build)
            fileless = await add_source(conn, name, uri=uri, kind="text", metadata={})
            nontext = await add_source(
                conn, name, uri=uri, kind="structured", metadata={"table": "t"}
            )
            await upsert_managed_source(
                conn, name, uri=uri, kind="text", files={"a.txt": {"context": {}}}
            )
            listed, _ = await list_sources(conn, name, limit=10)
            managed = [s for s in listed if s.uri == uri]
            assert {s.id for s in managed} == {fileless.id, nontext.id}  # both preserved
            # ...and BOTH are now canonical managed-text carrying the files stash —
            # no stale fileless/non-text row is left for the build to trip over
            assert all(s.kind == "text" for s in managed)
            assert all(
                s.metadata.get(MANAGED_FILES_KEY) == {"a.txt": {"context": {}}} for s in managed
            )
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


async def test_delete_project_purges_carryforward_state(migrated: None) -> None:
    """review_ledger / ontology_proposals / pipeline_runs are project-keyed
    with no FK to projects and carry forward across rebuilds by design — on a
    project DELETE they must be purged, or a recreated same-name project would
    silently inherit old review/proposal decisions (the completeness twin of
    the build guard)."""
    engine = _engine()
    name = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=name)
            await conn.execute(
                review_ledger.insert().values(
                    project=name,
                    target_kind="entity",
                    target_key="k1",
                    fingerprint_version=1,
                    decision="reject",
                    decided_by="test",
                )
            )
            await conn.execute(
                ontology_proposals.insert().values(
                    project=name,
                    kind="entity",
                    type_name="Foo",
                    proposal_key="pk1",
                    fingerprint_version=1,
                )
            )
            await conn.execute(
                pipeline_runs.insert().values(project=name, kind="source_validation")
            )

            assert await delete_project(conn, name) is True

            for tbl in (review_ledger, ontology_proposals, pipeline_runs):
                remaining = (
                    await conn.execute(sa.select(tbl.c.id).where(tbl.c.project == name))
                ).all()
                assert remaining == [], f"{tbl.name} not purged on project delete"
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
            await ensure_project(conn, name)
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
