"""Why: the index step (§5 step 5) is only correct if its projections land in
the REAL stores under the real constraints — a resolved graph MERGEd into
Neo4j, embeddings upserted into Qdrant, and the point ids written back into
Postgres — and if the active-bound readers then see EXACTLY the active build's
projection and nothing a prior/other build left behind (DR-006). The
orchestration decisions (active-only projection, dangling-relation skip,
idempotent re-run) are unit-tested with fakes in test_index_indexing.py; here
they run against live Postgres + Qdrant + Neo4j with a deterministic fake
embedder (no OpenAI key in CI) so the projection is real but reproducible.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from llama_index.core.embeddings import BaseEmbedding
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.index.indexing import index_build
from core.resolve import fingerprints
from core.stores.graph import BuildScopedGraphProjector, BuildScopedGraphRepo, graph_driver
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, chunks, documents, entities, relations
from core.stores.vectors import (
    BuildScopedVectorProjector,
    BuildScopedVectorRepo,
    collection_for,
    vector_client,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)

_WIPE_PROJECT = """\
MATCH (n:Entity {project: $project})
DETACH DELETE n
"""


class _FakeEmbedder:
    """Deterministic 4-dim vectors so projection is real but no OpenAI key is
    needed. The first component encodes text length so a point is retrievable
    by its own vector."""

    async def aget_text_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0, 0.0]


def _embedder() -> BaseEmbedding:
    """The fake, typed as the abstraction index_build expects (§3)."""
    return cast(BaseEmbedding, _FakeEmbedder())


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def stores(migrated: None) -> AsyncIterator[tuple[AsyncQdrantClient, AsyncSession]]:
    client = vector_client()
    driver = graph_driver()
    async with driver.session() as session:
        yield client, session
    await client.close()
    await driver.close()


async def _new_build(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _doc_with_chunks(writer: BuildScopedWriter, content_hash: str, texts: list[str]) -> None:
    doc_id = uuid.uuid4()
    await writer.insert(
        documents,
        id=doc_id,
        source_uri=f"s://{content_hash}",
        content_hash=content_hash,
        mime="text/plain",
        ingested_at=NOW,
    )
    offset = 0
    for ordinal, text in enumerate(texts):
        await writer.insert(
            chunks,
            id=uuid.uuid4(),
            document_id=doc_id,
            ordinal=ordinal,
            text=text,
            start_offset=offset,
            end_offset=offset + len(text),
        )
        offset += len(text)


async def _entity(
    writer: BuildScopedWriter, etype: str, name: str, *, status: str = "active"
) -> tuple[uuid.UUID, str]:
    key = fingerprints.entity_key(etype, name)
    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type=etype,
        canonical_name=name,
        entity_key=key,
        status=status,
        review_status="unreviewed",
        created_by="rule",
        created_at=NOW,
        updated_at=NOW,
    )
    return entity_id, key


async def _relation(
    writer: BuildScopedWriter,
    src: tuple[uuid.UUID, str],
    rtype: str,
    dst: tuple[uuid.UUID, str],
) -> uuid.UUID:
    relation_id = uuid.uuid4()
    await writer.insert(
        relations,
        id=relation_id,
        src_entity_id=src[0],
        dst_entity_id=dst[0],
        type=rtype,
        relation_signature=fingerprints.relation_signature(src[1], rtype, dst[1]),
        status="active",
        review_status="unreviewed",
        created_by="rule",
        confidence=1.0,
        created_at=NOW,
        updated_at=NOW,
    )
    return relation_id


async def _projectors(
    conn: AsyncConnection,
    client: AsyncQdrantClient,
    session: AsyncSession,
    writer: BuildScopedWriter,
) -> tuple[BuildScopedVectorProjector, BuildScopedGraphProjector]:
    vectors = await BuildScopedVectorProjector.for_building_build(
        conn, client, writer.project, writer.build_id
    )
    graph = await BuildScopedGraphProjector.for_building_build(
        conn, session, writer.project, writer.build_id
    )
    return vectors, graph


async def _cleanup(client: AsyncQdrantClient, session: AsyncSession, project: str) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    await (await session.run(_WIPE_PROJECT, {"project": project})).consume()
    engine = _engine()
    async with engine.connect() as conn:
        # entities cascade to relations/mentions/merge_candidates; documents to chunks
        await conn.execute(entities.delete().where(entities.c.project == project))
        await conn.execute(documents.delete().where(documents.c.project == project))
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()


async def test_index_projects_active_graph_and_embeddings_end_to_end(
    stores: tuple[AsyncQdrantClient, AsyncSession],
) -> None:
    """The full C5 arc on live stores: only ``active`` entities/relations
    reach Neo4j and Qdrant, a relation whose endpoint was rejected is SKIPPED
    (no node to attach to), point ids are written back into Postgres, and a
    re-run converges (§5) — no new points, the graph re-MERGEd."""
    client, session = stores
    engine = _engine()
    project = f"xtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            writer = await _new_build(conn, project)
            await _doc_with_chunks(writer, "h1", ["alpha chunk", "beta"])
            acme = await _entity(writer, "Company", "Acme")
            globex = await _entity(writer, "Company", "Globex")
            ghost = await _entity(writer, "Company", "Ghost", status="rejected")
            await _relation(writer, acme, "PARTNERS", globex)  # both active → projects
            await _relation(writer, acme, "OWNS", ghost)  # endpoint rejected → skipped
            await conn.commit()

            vectors, graph = await _projectors(conn, client, session, writer)
            report = await index_build(writer, _embedder(), vectors, graph)
            await conn.commit()

            assert (report.chunks_embedded, report.entities_embedded) == (2, 2)
            assert (report.entities_projected, report.relations_projected) == (2, 1)
            assert report.relations_skipped == 1

            # re-run converges (§5) while STILL building — no re-embed (point ids
            # set), graph re-MERGEd idempotently, no new §18 work items
            vectors_again, graph_again = await _projectors(conn, client, session, writer)
            again = await index_build(writer, _embedder(), vectors_again, graph_again)
            await conn.commit()
            assert (again.chunks_embedded, again.entities_embedded) == (0, 0)
            assert again.entities_projected == 2 and again.relations_projected == 1
            assert again.outcomes == ()

            await conn.execute(
                builds.update().where(builds.c.id == writer.build_id).values(status="active")
            )
            await conn.commit()

            # --- Qdrant: only the two active entities + the two chunks exist ---
            reader = await BuildScopedVectorRepo.for_active_build(conn, client, project)
            assert await reader.point_count("entity") == 2
            assert await reader.point_count("chunk") == 2
            assert await reader.point_count() == 4
            # the rejected entity was never embedded (its point would be a 5th)
            hits = await reader.search(
                [float(len("Acme")), 1.0, 0.0, 0.0], limit=10, point_type="entity"
            )
            assert {h.payload["canonical_id"] for h in hits if h.payload} == {
                str(acme[0]),
                str(globex[0]),
            }

            # --- Neo4j: two nodes, one edge; the dangling relation is absent ---
            graph_reader = await BuildScopedGraphRepo.for_active_build(conn, session, project)
            assert await graph_reader.entity_count() == 2
            assert await graph_reader.relation_count() == 1
            assert {e["canonical_id"] for e in await graph_reader.fetch_entities()} == {
                str(acme[0]),
                str(globex[0]),
            }

            # --- Postgres: point ids written back on the active rows only ------
            active_entity_rows = (
                await conn.execute(
                    entities.select().where(
                        entities.c.project == project, entities.c.status == "active"
                    )
                )
            ).fetchall()
            assert all(r.embedding_point_id == r.id for r in active_entity_rows)
            ghost_row = (
                await conn.execute(entities.select().where(entities.c.id == ghost[0]))
            ).one()
            assert ghost_row.embedding_point_id is None  # rejected → never embedded
            chunk_rows = (await conn.execute(chunks.select())).fetchall()
            assert chunk_rows and all(r.vector_point_id == r.id for r in chunk_rows)
            # the converged re-run above added no duplicate points (ids are the
            # row ids, so an upsert overwrites in place)
            assert await reader.point_count() == 4
    finally:
        await engine.dispose()
        await _cleanup(client, session, project)


async def test_two_builds_stay_isolated_after_index(
    stores: tuple[AsyncQdrantClient, AsyncSession],
) -> None:
    """DR-006 through the C5 orchestration: two builds' projections coexist in
    the shared Qdrant collection and Neo4j database; the active-bound readers
    see ONLY the active build's points and nodes, never the archived build's."""
    client, session = stores
    engine = _engine()
    project = f"xtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            old = await _new_build(conn, project)
            await _doc_with_chunks(old, "old-h", ["old only"])
            await _entity(old, "Company", "OldCo")
            await conn.commit()
            v_old, g_old = await _projectors(conn, client, session, old)
            await index_build(old, _embedder(), v_old, g_old)
            await conn.commit()

            new = await _new_build(conn, project)
            await _doc_with_chunks(new, "new-h", ["new one", "new two"])
            await _entity(new, "Company", "NewCo")
            await _entity(new, "Company", "NewCo2")
            await conn.commit()
            v_new, g_new = await _projectors(conn, client, session, new)
            await index_build(new, _embedder(), v_new, g_new)
            await conn.commit()

            await conn.execute(
                builds.update().where(builds.c.id == new.build_id).values(status="active")
            )
            await conn.execute(
                builds.update().where(builds.c.id == old.build_id).values(status="archived")
            )
            await conn.commit()

            reader = await BuildScopedVectorRepo.for_active_build(conn, client, project)
            assert reader.build_id == new.build_id
            assert await reader.point_count("entity") == 2  # NewCo + NewCo2, not OldCo
            assert await reader.point_count("chunk") == 2  # new's two chunks, not old's one

            graph_reader = await BuildScopedGraphRepo.for_active_build(conn, session, project)
            assert await graph_reader.entity_count() == 2
            names = {e["name"] for e in await graph_reader.fetch_entities()}
            assert names == {"NewCo", "NewCo2"} and "OldCo" not in names
    finally:
        await engine.dispose()
        await _cleanup(client, session, project)
