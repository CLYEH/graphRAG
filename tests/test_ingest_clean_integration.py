"""Why: §5's first two steps are only real if payloads land as build-scoped
rows on live Postgres THROUGH the DR-006 writer, and if re-running ingest
converges instead of duplicating — idempotency by content_hash is the §18
promise that makes 'retry failed only' safe to run wholesale. Chunk rows
must land linked, ordered, and offset-exact, because §27.4 evidence spans
will point into them.
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

from core.clean.chunking import InconsistentChunksError, clean_document
from core.config import get_settings
from core.ingest.connectors import DocumentPayload
from core.ingest.documents import ingest_documents
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, chunks, documents

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _building_writer(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def test_ingest_then_clean_lands_scoped_linked_rows(migrated: None) -> None:
    """The full §5 step-1→2 flow: payloads become scoped document rows, each
    document's chunks land linked to it with sequential ordinals and offsets
    that slice the raw text exactly."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    text = "First paragraph about Alice. " * 30  # long enough for several chunks
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _building_writer(conn, project)
            report = await ingest_documents(
                writer,
                [
                    DocumentPayload("mem://a", text, "text/plain", {"filename": "a"}),
                    DocumentPayload("mem://b", "tiny", "text/plain"),
                ],
            )
            assert [o.status for o in report.outcomes] == ["ingested", "ingested"]

            for ingested in report.documents:
                await clean_document(
                    writer, ingested.document_id, ingested.raw, max_chars=120, overlap=20
                )

            doc_rows = await writer.fetch_all(documents)
            assert {row.status for row in doc_rows} == {"ingested"}
            big = next(r for r in doc_rows if r.source_uri == "mem://a")
            chunk_rows = await writer.fetch_all(chunks, chunks.c.document_id == big.id)
            ordered = sorted(chunk_rows, key=lambda r: r.ordinal)
            assert [r.ordinal for r in ordered] == list(range(len(ordered)))
            assert len(ordered) > 1
            for row in ordered:
                assert text[row.start_offset : row.end_offset] == row.text
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_recleaning_converges_or_refuses_to_mix_chunkings(migrated: None) -> None:
    """§27.7 retry re-invokes clean for a document whose rows already exist:
    same params → converge (same chunks back, no duplicate-ordinal crash);
    different params → the stored and computed chunkings disagree, and mixing
    them would leave evidence offsets pointing at text retrieval doesn't
    serve — refused typed."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    text = "Sentence one here. " * 40
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _building_writer(conn, project)
            report = await ingest_documents(
                writer, [DocumentPayload("mem://a", text, "text/plain")]
            )
            (doc,) = report.documents
            first = await clean_document(
                writer, doc.document_id, doc.raw, max_chars=100, overlap=10
            )
            again = await clean_document(
                writer, doc.document_id, doc.raw, max_chars=100, overlap=10
            )
            assert again == first  # converged, no duplicate rows
            rows = await writer.fetch_all(chunks, chunks.c.document_id == doc.document_id)
            assert len(rows) == len(first)

            with pytest.raises(InconsistentChunksError):
                await clean_document(writer, doc.document_id, doc.raw, max_chars=60, overlap=10)
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_reingesting_converges_instead_of_duplicating(migrated: None) -> None:
    """§5 冪等/§18: the same payloads run twice (a wholesale retry after a
    partial failure) must yield skipped outcomes and NO new rows — identity
    is content_hash, not source_uri, so a moved file is still one document."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _building_writer(conn, project)
            first = await ingest_documents(
                writer,
                [
                    DocumentPayload("mem://a", "same content", "text/plain"),
                    DocumentPayload("mem://moved", "same content", "text/plain"),  # dup in-batch
                ],
            )
            assert [o.status for o in first.outcomes] == ["ingested", "skipped"]

            second = await ingest_documents(
                writer, [DocumentPayload("mem://a", "same content", "text/plain")]
            )
            assert [o.status for o in second.outcomes] == ["skipped"]
            assert second.documents == ()
            count = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(documents)
                    .where(documents.c.build_id == writer.build_id)
                )
            ).scalar_one()
            assert count == 1
            await trans.rollback()
    finally:
        await engine.dispose()
