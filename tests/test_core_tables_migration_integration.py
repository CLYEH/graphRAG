"""Why: §27.4's evidence rules and the build-scoped referential topology are
only real if the rendered DDL enforces them on actual Postgres — a CHECK that
exists in metadata but not in the migration is writer discipline in disguise
(same pattern as the builds/observability integration tests).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.stores.tables import (
    documents,
    entities,
    entity_mentions,
    relation_evidence,
    relations,
)

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


async def _insert_entity(conn: AsyncConnection, build_id: uuid.UUID) -> uuid.UUID:
    entity_id: uuid.UUID = (
        await conn.execute(
            entities.insert()
            .values(
                project="itest-x",
                build_id=build_id,
                type="person",
                canonical_name="Ada",
                entity_key=f"fpv1:{uuid.uuid4().hex}",
                status="active",
            )
            .returning(entities.c.id)
        )
    ).scalar_one()
    return entity_id


async def _insert_relation(conn: AsyncConnection, build_id: uuid.UUID) -> uuid.UUID:
    src, dst = await _insert_entity(conn, build_id), await _insert_entity(conn, build_id)
    relation_id: uuid.UUID = (
        await conn.execute(
            relations.insert()
            .values(
                project="itest-x",
                build_id=build_id,
                src_entity_id=src,
                dst_entity_id=dst,
                type="knows",
                status="active",
            )
            .returning(relations.c.id)
        )
    ).scalar_one()
    return relation_id


async def test_status_outside_the_frozen_lifecycle_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="entities_status_valid"):
                await conn.execute(
                    entities.insert().values(
                        project="itest-x",
                        build_id=uuid.uuid4(),
                        type="person",
                        canonical_name="Ada",
                        entity_key="fpv1:x",
                        status="archived",  # a builds status, not a lifecycle one
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_chunk_evidence_without_a_span_is_impossible(migrated: None) -> None:
    """§27.4: chunk evidence must carry its extraction offsets — the exact
    invariant §16's source_refs minimums rely on downstream."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build = uuid.uuid4()
            relation_id = await _insert_relation(conn, build)
            with pytest.raises(IntegrityError, match="relation_evidence_chunk_has_span"):
                await conn.execute(
                    relation_evidence.insert().values(
                        relation_id=relation_id,
                        build_id=build,
                        evidence_type="chunk",
                        chunk_id=uuid.uuid4(),
                        quote="q",
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_manual_evidence_with_a_span_is_impossible(migrated: None) -> None:
    """§27.4: manual evidence is deliberately span-less — offsets on it would
    fake an extraction span no pipeline produced."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build = uuid.uuid4()
            relation_id = await _insert_relation(conn, build)
            with pytest.raises(IntegrityError, match="relation_evidence_manual_spanless"):
                await conn.execute(
                    relation_evidence.insert().values(
                        relation_id=relation_id,
                        build_id=build,
                        evidence_type="manual",
                        quote="q",
                        start_offset=0,
                        end_offset=1,
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_duplicate_evidence_hash_is_impossible_within_a_build(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build = uuid.uuid4()
            relation_id = await _insert_relation(conn, build)
            row = {
                "relation_id": relation_id,
                "build_id": build,
                "evidence_type": "chunk",
                "chunk_id": uuid.uuid4(),
                "start_offset": 0,
                "end_offset": 9,
                "quote": "q",
                "evidence_hash": "h1",
            }
            await conn.execute(relation_evidence.insert().values(**row))
            with pytest.raises(IntegrityError, match="relation_evidence_dedup"):
                await conn.execute(relation_evidence.insert().values(**row))
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_evidence_survives_chunk_deletion(migrated: None) -> None:
    """§27.4 prune survival, executed: deleting the quoted chunk's document
    (the prune path) must leave the evidence row intact with its denormalized
    quote/offsets/source_uri — chunk_id dangles by design."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build = uuid.uuid4()
            document_id = (
                await conn.execute(
                    documents.insert()
                    .values(
                        project="itest-x",
                        build_id=build,
                        source_uri="s3://bucket/doc",
                        content_hash="c1",
                    )
                    .returning(documents.c.id)
                )
            ).scalar_one()
            relation_id = await _insert_relation(conn, build)
            await conn.execute(
                relation_evidence.insert().values(
                    relation_id=relation_id,
                    build_id=build,
                    evidence_type="chunk",
                    chunk_id=uuid.uuid4(),  # stands in for a pruned old chunk
                    start_offset=0,
                    end_offset=9,
                    quote="the quote outlives the chunk",
                    source_uri="s3://bucket/doc",
                    evidence_hash="h-survives",
                )
            )
            await conn.execute(documents.delete().where(documents.c.id == document_id))
            surviving = (
                await conn.execute(
                    select(func.count())
                    .select_from(relation_evidence)
                    .where(relation_evidence.c.build_id == build)
                )
            ).scalar_one()
            assert surviving == 1
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_deleting_an_entity_cascades_through_the_graph(migrated: None) -> None:
    """Build pruning (C9) is a plain DELETE: mentions, relations, and evidence
    hang off entities via CASCADE, so removing an entity leaves no orphans."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build = uuid.uuid4()
            relation_id = await _insert_relation(conn, build)
            src = (
                await conn.execute(
                    select(relations.c.src_entity_id).where(relations.c.id == relation_id)
                )
            ).scalar_one()
            await conn.execute(
                entity_mentions.insert().values(
                    entity_id=src, source_kind="text", surface_form="Ada"
                )
            )
            await conn.execute(
                relation_evidence.insert().values(
                    relation_id=relation_id,
                    build_id=build,
                    evidence_type="row",
                    evidence_ref="people:42",
                )
            )
            await conn.execute(entities.delete().where(entities.c.id == src))
            for table in (relations, relation_evidence):
                left = (
                    await conn.execute(
                        select(func.count()).select_from(table).where(table.c.build_id == build)
                    )
                ).scalar_one()
                assert left == 0, table.name
            mentions_left = (
                await conn.execute(
                    select(func.count())
                    .select_from(entity_mentions)
                    .where(entity_mentions.c.entity_id == src)
                )
            ).scalar_one()
            assert mentions_left == 0
            await trans.rollback()
    finally:
        await engine.dispose()
