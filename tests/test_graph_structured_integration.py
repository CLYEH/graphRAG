"""Why: C3a is only real if extraction writes land as build-scoped rows on live
Postgres through the DR-006 writer, satisfy the frozen entity/relation/evidence
constraints, and STAY inside their build. The mention path is new fenced-module
surface (entity_mentions has no build_id — it is scoped through its parent
entity), so its guard must be proven on real infra: a mention cannot attach to
another build's entity, and it cannot land after the build activates (TOCTOU),
exactly like every other §27.1 write.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.graph.ontology import EntityRule, RelationRule, StructuredMapping
from core.graph.structured import extract_structured, row_source_ref
from core.ingest.connectors import DocumentPayload
from core.ingest.documents import ingest_documents
from core.resolve import fingerprints
from core.stores.repo import (
    BuildNotWritableError,
    BuildScopedWriter,
    MentionTargetNotInBuildError,
)
from core.stores.tables import builds, entities, entity_mentions, relation_evidence, relations

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAPPING = {
    "people": StructuredMapping(
        table="people",
        entities={
            "person": EntityRule("Person", "name", disambiguator_column="id"),
            "company": EntityRule("Company", "employer"),
        },
        relations=(RelationRule("WORKS_AT", src="person", dst="company"),),
    )
}


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _new_build(conn: AsyncConnection, project: str, status: str = "building") -> uuid.UUID:
    return (  # type: ignore[no-any-return]
        await conn.execute(
            builds.insert().values(project=project, status=status).returning(builds.c.id)
        )
    ).scalar_one()


async def _writer(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> BuildScopedWriter:
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _ingest_row(writer: BuildScopedWriter, pk: str, **cols: str) -> None:
    await ingest_documents(
        writer,
        [
            DocumentPayload(
                source_uri=f"mem://people/{pk}",
                raw=json.dumps({"id": pk, **cols}, sort_keys=True),
                mime="application/json",
                metadata={"table": "people", "pk": pk},
            )
        ],
    )


async def test_structured_extraction_lands_scoped_graph(migrated: None) -> None:
    """Two rows, one shared employer: the company collapses to one entity by
    exact key, both people are distinct (disambiguator), the WORKS_AT edge is
    one relation with two row-evidences, and every row is a mention."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build_id = await _new_build(conn, project)
            writer = await _writer(conn, project, build_id)
            await _ingest_row(writer, "1", name="Alice", employer="Acme")
            await _ingest_row(writer, "2", name="Bob", employer="Acme")

            report = await extract_structured(writer, _MAPPING)
            assert report.entities == 3  # Alice, Bob, Acme
            assert report.relations == 2  # Alice→Acme and Bob→Acme are distinct edges

            entity_rows = await writer.fetch_all(entities)
            assert {r.type for r in entity_rows} == {"Person", "Company"}
            assert all(r.entity_key and r.created_by == "rule" for r in entity_rows)
            acme = next(r for r in entity_rows if r.type == "Company")
            assert acme.entity_key == fingerprints.entity_key("Company", "Acme")

            # Acme is mentioned by BOTH rows; each person once
            mention_rows = (
                await conn.execute(
                    sa.select(entity_mentions).where(entity_mentions.c.entity_id == acme.id)
                )
            ).fetchall()
            assert {m.source_ref for m in mention_rows} == {
                row_source_ref("people", "1"),
                row_source_ref("people", "2"),
            }

            # two distinct WORKS_AT edges (Alice→Acme, Bob→Acme), each 1 evidence
            assert len(await writer.fetch_all(relations)) == 2
            ev = await writer.fetch_all(relation_evidence)
            assert len(ev) == 2 and all(e.evidence_type == "row" for e in ev)
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_rerun_writes_nothing_new(migrated: None) -> None:
    """§5: a wholesale re-run of extraction over the same build reuses every
    row — the second pass reports all-zero and the counts don't move."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build_id = await _new_build(conn, project)
            writer = await _writer(conn, project, build_id)
            await _ingest_row(writer, "1", name="Alice", employer="Acme")

            first = await extract_structured(writer, _MAPPING)
            second = await extract_structured(writer, _MAPPING)
            assert (first.entities, first.relations, first.mentions, first.evidence) == (2, 1, 2, 1)
            assert (second.entities, second.relations, second.mentions, second.evidence) == (
                0,
                0,
                0,
                0,
            )
            assert len(await writer.fetch_all(entities)) == 2
            mentions = (
                await conn.execute(sa.select(sa.func.count()).select_from(entity_mentions))
            ).scalar_one()
            assert mentions == 2
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_two_builds_stay_isolated(migrated: None) -> None:
    """The same source in two builds yields independent entity rows — the
    unique key is (project, build_id, entity_key), so nothing crosses."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            for _ in range(2):
                build_id = await _new_build(conn, project)
                writer = await _writer(conn, project, build_id)
                await _ingest_row(writer, "1", name="Alice", employer="Acme")
                await extract_structured(writer, _MAPPING)
            total = (
                await conn.execute(sa.select(sa.func.count()).select_from(entities))
            ).scalar_one()
            assert total == 4  # 2 entities x 2 builds, no collision
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_mention_cannot_attach_to_another_builds_entity(migrated: None) -> None:
    """The fenced mention guard: a writer bound to build B cannot hang a
    mention off an entity that lives in build A, even with a valid entity id —
    and the error names the real cause (parent not in scope), not a
    self-contradictory 'build not building' when B is perfectly building."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build_a = await _new_build(conn, project)
            writer_a = await _writer(conn, project, build_a)
            await _ingest_row(writer_a, "1", name="Alice", employer="Acme")
            await extract_structured(writer_a, _MAPPING)
            alice = next(r for r in await writer_a.fetch_all(entities) if r.type == "Person")

            build_b = await _new_build(conn, project)
            writer_b = await _writer(conn, project, build_b)
            with pytest.raises(MentionTargetNotInBuildError):
                await writer_b.insert_entity_mention(
                    entity_id=alice.id,
                    source_kind="structured",
                    source_ref="people:1",
                    surface_form="Alice",
                    confidence=1.0,
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_mention_refused_after_activation(migrated: None) -> None:
    """TOCTOU: a writer bound while the build was 'building' must not keep
    writing mentions after the build activates — same per-statement guarantee
    as insert()."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            build_id = await _new_build(conn, project)
            writer = await _writer(conn, project, build_id)
            await _ingest_row(writer, "1", name="Alice", employer="Acme")
            await extract_structured(writer, _MAPPING)
            alice = next(r for r in await writer.fetch_all(entities) if r.type == "Person")

            await conn.execute(
                builds.update().where(builds.c.id == build_id).values(status="active")
            )
            with pytest.raises(BuildNotWritableError):
                await writer.insert_entity_mention(
                    entity_id=alice.id,
                    source_kind="structured",
                    source_ref="people:extra",
                    surface_form="Alice",
                    confidence=1.0,
                )
            await trans.rollback()
    finally:
        await engine.dispose()
