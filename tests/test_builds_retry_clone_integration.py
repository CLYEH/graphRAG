"""Why: RB1-retry-core seeds the child build by CLONING the parent's documents,
and the build_id-scoped tables key rows by a standalone ``id`` PK (DR-006), so
the clone must mint FRESH ids (never an ``UPDATE build_id`` — that would move the
parent's row, not copy it) with ``build_id`` set to the child. These tests prove
over live SQL that the child gets its own documents (fresh ids, child build_id,
content carried), the parent is untouched (audit integrity), and NEITHER chunks
NOR the graph layer are cloned — the child re-chunks fresh (cloning chunks would
risk InconsistentChunksError on a chunk-config change) and re-derives the graph
(cloning + re-running graph would drift/grow it). Cloning documents is enough:
``ingest`` dedups them by content_hash and hands them to ``clean``.
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

from core.builds.creation import mark_build_failed
from core.builds.retry import clone_raw_artifacts
from core.config import get_settings
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
    name = f"retry-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        # documents cascade their chunks; entities cascade their mentions
        await conn.execute(tables.documents.delete().where(tables.documents.c.project == name))
        await conn.execute(tables.entities.delete().where(tables.entities.c.project == name))
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


async def _seed_document(
    conn: AsyncConnection, project: str, build_id: uuid.UUID, content_hash: str, source_uri: str
) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                tables.documents.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    source_uri=source_uri,
                    raw="the raw text",
                    content_hash=content_hash,
                    mime="text/plain",
                    status="ingested",
                )
                .returning(tables.documents.c.id)
            )
        ).scalar_one(),
    )


async def _seed_chunk(
    conn: AsyncConnection,
    document_id: uuid.UUID,
    build_id: uuid.UUID,
    ordinal: int,
    *,
    vector_point_id: uuid.UUID | None,
) -> None:
    await conn.execute(
        tables.chunks.insert().values(
            document_id=document_id,
            build_id=build_id,
            ordinal=ordinal,
            text=f"chunk {ordinal}",
            start_offset=ordinal * 10,
            end_offset=ordinal * 10 + 9,
            vector_point_id=vector_point_id,
            status="cleaned",
        )
    )


async def _seed_entity(conn: AsyncConnection, project: str, build_id: uuid.UUID, key: str) -> None:
    await conn.execute(
        tables.entities.insert().values(
            project=project,
            build_id=build_id,
            type="Person",
            canonical_name="Ada",
            entity_key=key,
            status="active",
            created_by="llm",
        )
    )


async def test_mark_build_failed_only_terminalizes_a_building_build(project: str) -> None:
    # Codex #100 P1 R5 helper: the worker calls this to reclaim a pre-created
    # retry child stranded 'building' on a preflight failure. It must flip a
    # 'building' build to 'failed' but NEVER clobber an already-terminal build
    # (the status='building' guard — a stale-lease race must not overwrite a
    # 'ready'/'active' result).
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            building = await _make_build(conn, project)  # status='building'
            ready = cast(
                "uuid.UUID",
                (
                    await conn.execute(
                        tables.builds.insert()
                        .values(project=project, status="ready")
                        .returning(tables.builds.c.id)
                    )
                ).scalar_one(),
            )
            await conn.commit()

            await mark_build_failed(conn, building)
            await mark_build_failed(conn, ready)  # guarded: a no-op on a terminal build
            await conn.commit()

            status_by_id = {
                r.id: r.status
                for r in (
                    await conn.execute(
                        tables.builds.select().where(tables.builds.c.project == project)
                    )
                ).all()
            }
            assert status_by_id[building] == "failed"
            assert status_by_id[ready] == "ready"  # untouched
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_clone_copies_documents_with_fresh_ids_and_leaves_parent_untouched(
    project: str,
) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            parent = await _make_build(conn, project)
            child = await _make_build(conn, project)

            # parent raw layer: two docs, plus chunks + an entity that must NOT
            # be cloned (the child re-chunks and re-extracts them fresh).
            doc_a = await _seed_document(conn, project, parent, "hash-a", "file:///a.txt")
            doc_b = await _seed_document(conn, project, parent, "hash-b", "file:///b.txt")
            await _seed_chunk(conn, doc_a, parent, 0, vector_point_id=uuid.uuid4())
            await _seed_chunk(conn, doc_b, parent, 0, vector_point_id=None)
            await _seed_entity(conn, project, parent, "fpv2:ada")
            await conn.commit()

            counts = await clone_raw_artifacts(conn, project, parent, child)
            await conn.commit()

            assert counts.documents == 2

            # --- child documents: same content, FRESH ids, build_id = child ---
            child_docs = (
                await conn.execute(
                    tables.documents.select().where(tables.documents.c.build_id == child)
                )
            ).all()
            assert {d.content_hash for d in child_docs} == {"hash-a", "hash-b"}
            child_doc_ids = {d.id for d in child_docs}
            # fresh ids: a clone that reused parent ids (or moved the row) would
            # collide with the parent's rows or strip them from the parent build
            assert doc_a not in child_doc_ids and doc_b not in child_doc_ids
            assert all(d.project == project for d in child_docs)
            child_by_hash = {d.content_hash: d for d in child_docs}
            assert child_by_hash["hash-a"].source_uri == "file:///a.txt"
            assert child_by_hash["hash-a"].raw == "the raw text"

            # --- chunks + graph layer NOT cloned: the child re-derives them ---
            child_chunks = (
                await conn.execute(tables.chunks.select().where(tables.chunks.c.build_id == child))
            ).all()
            assert child_chunks == []
            child_entities = (
                await conn.execute(
                    tables.entities.select().where(tables.entities.c.build_id == child)
                )
            ).all()
            assert child_entities == []

            # --- parent untouched (audit integrity): its docs + chunks stay ---
            parent_docs = (
                await conn.execute(
                    tables.documents.select().where(tables.documents.c.build_id == parent)
                )
            ).all()
            assert {d.id for d in parent_docs} == {doc_a, doc_b}
            parent_chunks = (
                await conn.execute(tables.chunks.select().where(tables.chunks.c.build_id == parent))
            ).all()
            assert len(parent_chunks) == 2
            await conn.rollback()
    finally:
        await engine.dispose()
