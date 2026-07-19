"""Why: RB1-retry-skip reuses the parent's SUCCESSFUL graph-layer artifacts so a
retry re-calls the LLM only for the documents that failed graph extraction. The
clone must be SELECTIVE — copying only artifacts attributable to a NON-failed
document — because a failed document has PARTIAL committed rows in the parent, and
cloning those would let its full fresh re-extraction ADD drifted ghosts beside
them (the dedup index blocks exact dups, not drift). These tests prove over live
SQL that: only successful-doc entities/mentions/relations/evidence are copied
(fresh ids, identities preserved), a failed doc's artifacts are LEFT for
re-extraction, the copy has no duplicate mentions (the mention table has no DB
unique index), the clone is idempotent for resume, and the parent is untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.retry import clone_graph_artifacts, graph_entangles_failed_docs
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
    name = f"gclone-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
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


async def _entity(conn: AsyncConnection, project: str, build_id: uuid.UUID, key: str) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                tables.entities.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    type="Person",
                    canonical_name=key.split(":")[-1],
                    entity_key=key,
                    status="active",
                    created_by="llm",
                    embedding_point_id=uuid.uuid4(),  # MUST NOT be cloned
                )
                .returning(tables.entities.c.id)
            )
        ).scalar_one(),
    )


async def _mention(conn: AsyncConnection, entity_id: uuid.UUID, content_hash: str) -> None:
    await conn.execute(
        tables.entity_mentions.insert().values(
            entity_id=entity_id, source_kind="text", source_ref=f"chunk:{content_hash}:0"
        )
    )


async def _relation(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    src: uuid.UUID,
    dst: uuid.UUID,
    signature: str,
) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                tables.relations.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    src_entity_id=src,
                    dst_entity_id=dst,
                    type="KNOWS",
                    relation_signature=signature,
                    status="active",
                    created_by="llm",
                )
                .returning(tables.relations.c.id)
            )
        ).scalar_one(),
    )


async def _evidence(
    conn: AsyncConnection,
    relation_id: uuid.UUID,
    build_id: uuid.UUID,
    content_hash: str,
    evidence_hash: str,
) -> None:
    await conn.execute(
        tables.relation_evidence.insert().values(
            relation_id=relation_id,
            build_id=build_id,
            evidence_type="chunk",
            evidence_ref=f"chunk:{content_hash}:0",
            chunk_id=uuid.uuid4(),  # dangling-by-design; copied verbatim
            start_offset=0,
            end_offset=5,
            quote="quote",
            source_uri="file:///x.txt",
            evidence_hash=evidence_hash,
        )
    )


async def _seed_parent_graph(conn: AsyncConnection, project: str, parent: uuid.UUID) -> None:
    """A parent graph spanning a SUCCESSFUL doc (hash-A) and a FAILED doc (hash-B):
    a SHARED entity mentioned by both, an A-only and a B-only entity, an A-only
    relation (evidence from A) and a B-only relation (evidence from B)."""
    shared = await _entity(conn, project, parent, "fpv2:shared")
    a_only = await _entity(conn, project, parent, "fpv2:aonly")
    b_only = await _entity(conn, project, parent, "fpv2:bonly")
    await _mention(conn, shared, "hash-A")
    await _mention(conn, shared, "hash-B")  # same entity, from the FAILED doc
    await _mention(conn, a_only, "hash-A")
    await _mention(conn, b_only, "hash-B")
    a_rel = await _relation(conn, project, parent, shared, a_only, "sig-a")
    b_rel = await _relation(conn, project, parent, shared, b_only, "sig-b")
    await _evidence(conn, a_rel, parent, "hash-A", "eh-a")
    await _evidence(conn, b_rel, parent, "hash-B", "eh-b")


async def _child_state(conn: AsyncConnection, project: str, child: uuid.UUID) -> dict[str, object]:
    ents = (
        await conn.execute(tables.entities.select().where(tables.entities.c.build_id == child))
    ).all()
    ent_ids = {e.id for e in ents}
    mentions = (
        await conn.execute(
            tables.entity_mentions.select().where(
                tables.entity_mentions.c.entity_id.in_(ent_ids or {uuid.uuid4()})
            )
        )
    ).all()
    rels = (
        await conn.execute(tables.relations.select().where(tables.relations.c.build_id == child))
    ).all()
    evid = (
        await conn.execute(
            tables.relation_evidence.select().where(tables.relation_evidence.c.build_id == child)
        )
    ).all()
    return {"entities": ents, "mentions": mentions, "relations": rels, "evidence": evid}


async def test_clone_reuses_only_successful_doc_artifacts_and_leaves_failed_ones(
    project: str,
) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            parent = await _make_build(conn, project)
            child = await _make_build(conn, project)
            await _seed_parent_graph(conn, project, parent)
            await conn.commit()

            counts = await clone_graph_artifacts(
                conn, project, parent, child, frozenset({"hash-B"})
            )
            await conn.commit()

            # counts: shared + a_only entities, their 2 A-mentions, the A relation
            # + its A evidence — NOTHING from the failed doc B
            assert (counts.entities, counts.entity_mentions, counts.relations) == (2, 2, 1)
            assert counts.relation_evidence == 1

            state = await _child_state(conn, project, child)
            ents = cast(list[Any], state["entities"])
            # SHARED + AONLY cloned (identity preserved, FRESH ids, point-id dropped)
            assert {e.entity_key for e in ents} == {"fpv2:shared", "fpv2:aonly"}
            assert all(e.embedding_point_id is None for e in ents)  # re-embed, never cloned
            by_key = {e.entity_key: e for e in ents}

            # mentions: only the successful-doc (hash-A) refs — the SHARED entity's
            # hash-B mention (failed doc) is LEFT for re-extraction, no dup
            mentions = cast(list[Any], state["mentions"])
            assert {(m.entity_id, m.source_ref) for m in mentions} == {
                (by_key["fpv2:shared"].id, "chunk:hash-A:0"),
                (by_key["fpv2:aonly"].id, "chunk:hash-A:0"),
            }

            # relations: only the A relation, endpoints remapped to CHILD entities
            rels = cast(list[Any], state["relations"])
            assert len(rels) == 1
            assert rels[0].relation_signature == "sig-a"
            assert rels[0].src_entity_id == by_key["fpv2:shared"].id
            assert rels[0].dst_entity_id == by_key["fpv2:aonly"].id

            # evidence: only the A relation's A evidence (child relation + build)
            evid = cast(list[Any], state["evidence"])
            assert {(x.evidence_hash, x.relation_id) for x in evid} == {("eh-a", rels[0].id)}

            # parent untouched (audit integrity)
            parent_ents = (
                await conn.execute(
                    tables.entities.select().where(tables.entities.c.build_id == parent)
                )
            ).all()
            assert {e.entity_key for e in parent_ents} == {
                "fpv2:shared",
                "fpv2:aonly",
                "fpv2:bonly",
            }
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_clone_is_idempotent_for_resume(project: str) -> None:
    """A re-dispatched retry re-runs the clone; the NOT EXISTS identity guards
    (entities_by_key / relations_by_signature / relation_evidence_dedup) must make
    the second run a no-op, never a unique-index violation."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            parent = await _make_build(conn, project)
            child = await _make_build(conn, project)
            await _seed_parent_graph(conn, project, parent)
            await conn.commit()

            first = await clone_graph_artifacts(conn, project, parent, child, frozenset({"hash-B"}))
            await conn.commit()
            second = await clone_graph_artifacts(
                conn, project, parent, child, frozenset({"hash-B"})
            )
            await conn.commit()

            assert (first.entities, first.relations, first.relation_evidence) == (2, 1, 1)
            # second run copies NOTHING — every row already exists under the child
            assert (second.entities, second.entity_mentions) == (0, 0)
            assert (second.relations, second.relation_evidence) == (0, 0)

            state = await _child_state(conn, project, child)
            assert len(cast(list[Any], state["entities"])) == 2  # not doubled
            assert len(cast(list[Any], state["mentions"])) == 2
            assert len(cast(list[Any], state["relations"])) == 1
            assert len(cast(list[Any], state["evidence"])) == 1
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_empty_failed_set_clones_the_whole_text_graph(project: str) -> None:
    """A parent that failed AFTER graph (no graph-step failures) hands an EMPTY
    failed set — every text artifact is reused, none left behind."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            parent = await _make_build(conn, project)
            child = await _make_build(conn, project)
            await _seed_parent_graph(conn, project, parent)
            await conn.commit()

            counts = await clone_graph_artifacts(conn, project, parent, child, frozenset())
            await conn.commit()

            # all 3 entities, all 4 mentions, both relations, both evidence rows
            assert (counts.entities, counts.entity_mentions) == (3, 4)
            assert (counts.relations, counts.relation_evidence) == (2, 2)
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_graph_entangles_failed_docs_flags_an_entity_mentioned_by_both(project: str) -> None:
    """RB1-retry-skip round-3/4 guard: an ENTITY with a text mention from BOTH a
    failed doc AND a non-failed doc may carry the FAILED doc's first-write scalars
    (entity/relation rows are first-write-wins), which the selective clone retains
    and preload freezes — so the caller must full-re-derive. Checking entities
    subsumes the relation case (a chunk-evidence relation's endpoints are always
    mentioned by the same docs). Discriminating: True only when the entity genuinely
    spans the failed set; an entity mentioned solely by non-failed docs is reusable
    (False), and entanglement is relative to WHICH docs are failed."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            entangled = await _make_build(conn, project)
            clean = await _make_build(conn, project)
            # entangled: an entity mentioned by hash-A (success) AND hash-B (failed)
            shared = await _entity(conn, project, entangled, "fpv2:shared")
            await _mention(conn, shared, "hash-A")
            await _mention(conn, shared, "hash-B")
            # clean: an entity mentioned ONLY by the successful doc hash-A
            solo = await _entity(conn, project, clean, "fpv2:solo")
            await _mention(conn, solo, "hash-A")
            await conn.commit()

            assert await graph_entangles_failed_docs(conn, entangled, frozenset({"hash-B"})) is True
            # only success mention → reusable
            assert await graph_entangles_failed_docs(conn, clean, frozenset({"hash-B"})) is False
            # entanglement is relative to the failed set: neither of shared's docs is failed
            assert (
                await graph_entangles_failed_docs(conn, entangled, frozenset({"hash-Z"})) is False
            )
            await conn.rollback()
    finally:
        await engine.dispose()
