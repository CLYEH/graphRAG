"""Why: semantic_search is only correct if a REAL Qdrant kNN over a real build
returns the geometrically-nearest point (scoped to the active build) AND the
hit is enriched from live Postgres into a §16 result that actually validates
against the frozen contract — uri + offsets pulled from the chunk's document,
an entity's mention become its citation. The enrichment logic and drops are
unit-tested with fakes; here the whole path runs against live PG + Qdrant with
a deterministic embedder (no OpenAI key), so "the nearest point comes back,
cited, contract-valid, and only from the active build" is proven end to end.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from llama_index.core.embeddings import BaseEmbedding
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.index.indexing import index_build
from core.query.semantic import semantic_search
from core.resolve import fingerprints
from core.stores.graph import BuildScopedGraphProjector, graph_driver
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.tables import builds, chunks, documents, entities
from core.stores.vectors import (
    BuildScopedVectorProjector,
    BuildScopedVectorRepo,
    collection_for,
    vector_client,
)
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)

_SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text(encoding="utf-8")
)
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


class _FakeEmbedder:
    """Deterministic 8-dim vectors from sha256(text), so the SAME text (query
    vs stored) yields the SAME vector — an exact-text query is cosine-nearest
    to the point that stored that text — and distinct texts stay far apart.
    No OpenAI key, fully reproducible."""

    async def aget_text_embedding(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:8]]


def _embedder() -> BaseEmbedding:
    return cast(BaseEmbedding, _FakeEmbedder())


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def qdrant(migrated: None) -> AsyncIterator[AsyncQdrantClient]:
    client = vector_client()
    yield client
    await client.close()


async def _new_build(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    await ensure_project(conn, project)
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _chunk(writer: BuildScopedWriter, doc_id: uuid.UUID, ordinal: int, text: str) -> None:
    await writer.insert(
        chunks,
        id=uuid.uuid4(),
        document_id=doc_id,
        ordinal=ordinal,
        text=text,
        start_offset=ordinal * 100,
        end_offset=ordinal * 100 + len(text),
    )


async def _document(writer: BuildScopedWriter, content_hash: str, uri: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await writer.insert(
        documents,
        id=doc_id,
        source_uri=uri,
        content_hash=content_hash,
        mime="text/plain",
        ingested_at=NOW,
    )
    return doc_id


async def _entity_with_mention(writer: BuildScopedWriter, name: str, source_ref: str) -> uuid.UUID:
    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type="Team",
        canonical_name=name,
        entity_key=fingerprints.entity_key("Team", name),
        status="active",
        review_status="unreviewed",
        created_by="rule",
        created_at=NOW,
        updated_at=NOW,
    )
    await writer.insert_entity_mention(
        entity_id=entity_id,
        source_kind="text",
        source_ref=source_ref,
        surface_form=name,
        confidence=1.0,
    )
    return entity_id


async def _index(
    conn: AsyncConnection, client: AsyncQdrantClient, writer: BuildScopedWriter
) -> None:
    driver = graph_driver()
    try:
        async with driver.session() as session:
            vectors = await BuildScopedVectorProjector.for_building_build(
                conn, client, writer.project, writer.build_id
            )
            graph = await BuildScopedGraphProjector.for_building_build(
                conn, session, writer.project, writer.build_id
            )
            await index_build(writer, _embedder(), vectors, graph)
    finally:
        await driver.close()


async def _cleanup(client: AsyncQdrantClient, project: str) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    driver = graph_driver()
    try:
        async with driver.session() as session:
            await (
                await session.run("MATCH (n:Entity {project: $p}) DETACH DELETE n", {"p": project})
            ).consume()
    finally:
        await driver.close()
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(entities.delete().where(entities.c.project == project))
        await conn.execute(documents.delete().where(documents.c.project == project))
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()


async def test_semantic_search_returns_the_nearest_cited_result_end_to_end(
    qdrant: AsyncQdrantClient,
) -> None:
    """A real kNN over a real build: the exact-text query is nearest its own
    chunk point, and the hit comes back enriched with the document's source_uri
    + the chunk's offsets — a §16 result that validates against the frozen
    schema. The entity name query likewise returns the entity cited by its
    mention."""
    engine = _engine()
    project = f"qtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            writer = await _new_build(conn, project)
            doc = await _document(writer, "h1", "s3://acme/onboarding.md")
            await _chunk(writer, doc, 0, "alpha onboarding process")
            await _chunk(writer, doc, 1, "unrelated beta content")
            await _entity_with_mention(writer, "People Ops", "chunk:h1:0")
            await conn.commit()

            await _index(conn, qdrant, writer)
            await conn.commit()
            await conn.execute(
                builds.update().where(builds.c.id == writer.build_id).values(status="active")
            )
            await conn.commit()

            repo = await BuildScopedRepo.for_active_build(conn, project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)

            # --- chunk query: exact text is cosine-nearest its own point -------
            chunk_resp = await semantic_search(
                repo, vectors, _embedder(), "alpha onboarding process", top_k=5
            )
            payload = chunk_resp.to_dict()
            _VALIDATOR.validate(payload)
            top = payload["results"][0]
            assert top["result_type"] == "chunk"
            assert top["text"] == "alpha onboarding process"
            ref = top["source_refs"][0]
            assert ref["source_type"] == "chunk"
            assert ref["source_uri"] == "s3://acme/onboarding.md"
            assert ref["metadata"] == {"start_offset": 0, "end_offset": 24}
            assert payload["warnings"] == []  # everything was citable

            # --- entity query: name is nearest the entity point, cited by mention
            entity_resp = await semantic_search(repo, vectors, _embedder(), "People Ops", top_k=5)
            _VALIDATOR.validate(entity_resp.to_dict())
            entity_hit = next(r for r in entity_resp.results if r.result_type == "entity")
            assert entity_hit.title == "People Ops"
            assert entity_hit.source_refs[0].source_type == "chunk"
            assert entity_hit.source_refs[0].id == "chunk:h1:0"
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)


async def test_semantic_search_over_an_unindexed_build_is_empty_not_an_error(
    qdrant: AsyncQdrantClient,
) -> None:
    """The lazy-collection producer/consumer edge: a build that embedded
    nothing (index_build never called ensure_collection, so the project's
    Qdrant collection is absent) must still answer a semantic query with an
    empty, schema-valid response — not propagate Qdrant's collection-not-found
    error (§22)."""
    engine = _engine()
    project = f"qtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            writer = await _new_build(conn, project)
            await conn.commit()
            await _index(conn, qdrant, writer)  # nothing to embed → no collection
            await conn.commit()
            assert not await qdrant.collection_exists(collection_for(project))
            await conn.execute(
                builds.update().where(builds.c.id == writer.build_id).values(status="active")
            )
            await conn.commit()

            repo = await BuildScopedRepo.for_active_build(conn, project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            resp = await semantic_search(repo, vectors, _embedder(), "anything", top_k=5)
            _VALIDATOR.validate(resp.to_dict())
            assert resp.results == () and resp.warnings == ()
            assert (
                await vectors.point_count() == 0
            )  # count over the absent collection is 0, not 404
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)


async def test_a_rejected_entity_with_a_stale_point_is_dropped_not_surfaced(
    qdrant: AsyncQdrantClient,
) -> None:
    """SoR re-verification for entity status: the index projects active
    entities only, but projection is forward-only, so a point can outlive the
    entity's exclusion when resolution later rejects/merges it. A semantic hit
    on that stale point must be DROPPED as drift (§19/§22) — a rejected entity
    must never reappear as a production result — surfaced as PARTIAL_RESULTS,
    not emitted."""
    engine = _engine()
    project = f"qtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            writer = await _new_build(conn, project)
            entity_id = await _entity_with_mention(writer, "People Ops", "chunk:h1:0")
            await conn.commit()
            await _index(conn, qdrant, writer)  # point projected while active
            await conn.commit()
            # resolution rejects the entity AFTER indexing; the point survives
            await conn.execute(
                entities.update()
                .where(entities.c.id == entity_id)
                .values(status="rejected", review_status="rejected")
            )
            await conn.execute(
                builds.update().where(builds.c.id == writer.build_id).values(status="active")
            )
            await conn.commit()

            repo = await BuildScopedRepo.for_active_build(conn, project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            resp = await semantic_search(repo, vectors, _embedder(), "People Ops", top_k=5)
            _VALIDATOR.validate(resp.to_dict())
            assert resp.results == ()  # the rejected entity is not surfaced
            assert resp.warnings and resp.warnings[0].code == "PARTIAL_RESULTS"
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)


async def test_semantic_search_reads_only_the_active_build(
    qdrant: AsyncQdrantClient,
) -> None:
    """DR-006 end to end: an archived build's points coexist in the shared
    collection, but a query bound to the active build must never surface them —
    the same phrase indexed in both builds returns only the active build's
    chunk."""
    engine = _engine()
    project = f"qtest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            old = await _new_build(conn, project)
            old_doc = await _document(old, "old", "s3://old.md")
            await _chunk(old, old_doc, 0, "shared phrase")
            await conn.commit()
            await _index(conn, qdrant, old)
            await conn.commit()

            new = await _new_build(conn, project)
            new_doc = await _document(new, "new", "s3://new.md")
            await _chunk(new, new_doc, 0, "shared phrase")
            await conn.commit()
            await _index(conn, qdrant, new)
            await conn.commit()

            await conn.execute(
                builds.update().where(builds.c.id == new.build_id).values(status="active")
            )
            await conn.execute(
                builds.update().where(builds.c.id == old.build_id).values(status="archived")
            )
            await conn.commit()

            repo = await BuildScopedRepo.for_active_build(conn, project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            resp = await semantic_search(repo, vectors, _embedder(), "shared phrase", top_k=10)
            # both builds indexed the identical vector; only the active build's
            # chunk (cited by the active build's document) may come back
            assert [r.source_refs[0].source_uri for r in resp.results] == ["s3://new.md"]
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)
