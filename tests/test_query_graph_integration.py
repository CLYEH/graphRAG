"""Why: graph_query is only correct if a real traversal over a real projection
comes back as §16 results whose every citation traces to live SoR rows — and
never escapes the active build. The emission discipline is unit-tested with
fakes; here the whole path runs against live Postgres + Neo4j: entities /
mentions / relations / evidence written by the real writer, projected by the
real projector, traversed by the real templates. Build isolation and the
stale-projection drop are proven where they matter — on the stores.
"""

from __future__ import annotations

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
from neo4j import AsyncSession
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.query.graph import GraphQueryParams, graph_query
from core.query.policy import CYPHER_ALLOWED_CLAUSES, CYPHER_BLOCKED_MIN, TextToCypher
from core.resolve import fingerprints
from core.stores.graph import BuildScopedGraphProjector, BuildScopedGraphRepo, graph_driver
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.tables import builds, entities, relation_evidence, relations

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)

_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_POLICY = TextToCypher(
    enabled=False,  # templates are the default path; enabled gates only NL→Cypher
    allowed_clauses=CYPHER_ALLOWED_CLAUSES,
    blocked=CYPHER_BLOCKED_MIN,
    max_rows=50,
    timeout_ms=5000,
)

_WIPE_PROJECT = """\
MATCH (n:Entity {project: $project})
DETACH DELETE n
"""


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def stores(migrated: None) -> AsyncIterator[tuple[AsyncConnection, AsyncSession]]:
    engine = _engine()
    driver = graph_driver()
    async with engine.connect() as conn, driver.session() as session:
        yield conn, session
    await driver.close()
    await engine.dispose()


async def _new_build(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _entity(
    writer: BuildScopedWriter, name: str, *, mention_ref: str | None = None
) -> tuple[uuid.UUID, str]:
    key = fingerprints.entity_key("org", name)
    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type="org",
        canonical_name=name,
        entity_key=key,
        status="active",
        review_status="unreviewed",
        created_by="rule",
        created_at=NOW,
        updated_at=NOW,
    )
    await writer.insert_entity_mention(
        entity_id=entity_id,
        source_kind="text",
        source_ref=mention_ref or f"chunk-{entity_id}",
        surface_form=name,
        confidence=1.0,
    )
    return entity_id, key


async def _relation(
    writer: BuildScopedWriter,
    src: tuple[uuid.UUID, str],
    rtype: str,
    dst: tuple[uuid.UUID, str],
) -> uuid.UUID:
    relation_id = uuid.uuid4()
    signature = fingerprints.relation_signature(src[1], rtype, dst[1])
    await writer.insert(
        relations,
        id=relation_id,
        src_entity_id=src[0],
        dst_entity_id=dst[0],
        type=rtype,
        relation_signature=signature,
        status="active",
        review_status="unreviewed",
        created_by="rule",
        confidence=1.0,
        created_at=NOW,
        updated_at=NOW,
    )
    await writer.insert(
        relation_evidence,
        id=uuid.uuid4(),
        relation_id=relation_id,
        evidence_type="chunk",
        evidence_ref=f"ev-{relation_id}",
        chunk_id=uuid.uuid4(),
        start_offset=0,
        end_offset=10,
        quote="the quote",
        source_uri="file:///doc.txt",
        evidence_hash=fingerprints.evidence_hash(signature, f"ev-{relation_id}", "the quote"),
        created_at=NOW,
    )
    return relation_id


async def _project_all(
    conn: AsyncConnection, session: AsyncSession, writer: BuildScopedWriter
) -> None:
    projector = await BuildScopedGraphProjector.for_building_build(
        conn, session, writer.project, writer.build_id
    )
    for row in await writer.fetch_all(entities):
        await projector.project_entity(str(row.id), row.type, row.status, name=row.canonical_name)
    for row in await writer.fetch_all(relations):
        await projector.project_relation(str(row.src_entity_id), str(row.dst_entity_id), row.type)


async def _activate(conn: AsyncConnection, build_id: uuid.UUID) -> None:
    await conn.execute(builds.update().where(builds.c.id == build_id).values(status="active"))


async def _bound(
    conn: AsyncConnection, session: AsyncSession, project: str
) -> tuple[BuildScopedGraphRepo, BuildScopedRepo]:
    graph = await BuildScopedGraphRepo.for_active_build(conn, session, project)
    repo = await BuildScopedRepo.for_active_build(conn, project)
    return graph, repo


async def _cleanup(session: AsyncSession, project: str) -> None:
    engine = _engine()
    async with engine.connect() as conn:
        # entities first: builds has no cascading children, but deleting the
        # project's entities cascades mentions → relations → evidence
        await conn.execute(entities.delete().where(entities.c.project == project))
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()
    await (await session.run(_WIPE_PROJECT, {"project": project})).consume()


async def _chain_build(
    conn: AsyncConnection, session: AsyncSession, project: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A → works_with → B → supplies → C, projected and activated."""
    writer = await _new_build(conn, project)
    a = await _entity(writer, "Acme")
    b = await _entity(writer, "BobCo")
    c = await _entity(writer, "CarolInc")
    await _relation(writer, a, "works_with", b)
    await _relation(writer, b, "supplies", c)
    await conn.commit()
    await _project_all(conn, session, writer)
    await _activate(conn, writer.build_id)
    await conn.commit()
    return a[0], b[0], c[0]


async def test_neighbors_end_to_end_returns_cited_entities(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        _a, b, c = await _chain_build(conn, session, project)
        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="neighbors", entity="Acme", hops=2),
            "who is around acme",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.warnings == ()
        ids = [r.id for r in response.results]
        assert ids[0] == str(b) and set(ids) == {str(b), str(c)}  # nearest first, both hops
        assert all(r.result_type == "entity" and r.source_refs for r in response.results)
        assert response.results[0].source_refs[0].source_type == "chunk"  # the SoR mention
    finally:
        await _cleanup(session, project)


async def test_path_end_to_end_cites_every_edge(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        await _chain_build(conn, session, project)
        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="path", entity="Acme", other_entity="CarolInc", hops=3),
            "acme to carol",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        assert len(response.results) == 1
        path = response.results[0]
        assert path.result_type == "path"
        assert len(path.source_refs) == 2  # §27.2: one ref per edge
        assert all(ref.source_type == "relation" for ref in path.source_refs)
        assert path.text == "Acme -[works_with]-> BobCo -[supplies]-> CarolInc"
    finally:
        await _cleanup(session, project)


async def test_subgraph_emits_evidence_backed_relations(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        await _chain_build(conn, session, project)
        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="subgraph", entity="Acme", hops=2),
            "acme's neighborhood",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        by_type = {r.result_type for r in response.results}
        assert by_type == {"entity", "relation"}
        rel_results = [r for r in response.results if r.result_type == "relation"]
        assert len(rel_results) == 2  # both chain edges, each evidence-cited
        for rel in rel_results:
            ref = rel.source_refs[0]
            assert ref.source_type == "chunk" and ref.source_uri == "file:///doc.txt"
            assert ref.metadata["quote"] == "the quote"  # §27.4 auditable excerpt
    finally:
        await _cleanup(session, project)


async def test_the_traversal_reads_only_the_active_build(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    """Two builds of the same project coexist in the one Neo4j database
    (DR-004); the bound scope must keep the archived build's denser graph
    invisible — the DR-006 guarantee, on the live store."""
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        # old build: Acme also knows a fourth entity through an extra edge
        old = await _new_build(conn, project)
        old_a = await _entity(old, "Acme")
        old_x = await _entity(old, "OldOnly")
        await _relation(old, old_a, "works_with", old_x)
        await conn.commit()
        await _project_all(conn, session, old)
        await conn.execute(
            builds.update().where(builds.c.id == old.build_id).values(status="archived")
        )
        await conn.commit()

        _a, b, c = await _chain_build(conn, session, project)
        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="neighbors", entity="Acme", hops=2),
            "who is around acme",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        ids = {r.id for r in response.results}
        assert str(old_x[0]) not in ids
        assert ids == {str(b), str(c)}  # the active build's chain only
    finally:
        await _cleanup(session, project)


async def test_a_stale_shortest_path_yields_the_longer_active_path(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    """The projection's SHORTEST path can be stale (its relation rejected in
    the SoR after projection) while a longer fully-active path exists: the
    stale edge must be excluded and the pair retried — the exclusion predicate
    is pushed into the live shortestPath expansion — so the active path is
    returned, not PARTIAL_RESULTS for a connection the active graph has."""
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        a = await _entity(writer, "Acme")
        b = await _entity(writer, "BobCo")
        c = await _entity(writer, "CarolInc")
        await _relation(writer, a, "works_with", b)
        await _relation(writer, b, "supplies", c)
        shortcut = await _relation(writer, a, "shortcut", c)  # the 1-hop path
        await conn.commit()
        await _project_all(conn, session, writer)
        await _activate(conn, writer.build_id)
        await conn.commit()
        # the shortcut is rejected AFTER projection — Neo4j still has the edge
        await conn.execute(
            relations.update().where(relations.c.id == shortcut).values(status="rejected")
        )
        await conn.commit()

        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="path", entity="Acme", other_entity="CarolInc", hops=3),
            "acme to carol",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        assert len(response.results) == 1
        path = response.results[0]
        assert path.text == "Acme -[works_with]-> BobCo -[supplies]-> CarolInc"
        assert len(path.source_refs) == 2  # the ACTIVE 2-hop path, fully cited
        assert response.warnings == ()
    finally:
        await _cleanup(session, project)


async def test_a_drifted_entity_is_dropped_not_surfaced(
    stores: tuple[AsyncConnection, AsyncSession],
) -> None:
    """Forward-only projection: an entity moved OFF active in the SoR after
    projection still has a stale-active node in Neo4j. The SoR re-verification
    must drop it AND everything reachable only THROUGH it — in the active
    graph, C hangs off the rejected B, so neither is a valid neighbor of A —
    and surface PARTIAL_RESULTS (§19/§22, the C6a lesson on the graph face)."""
    conn, session = stores
    project = f"graphq-{uuid.uuid4().hex[:10]}"
    try:
        _a, b, c = await _chain_build(conn, session, project)
        # resolution moves B off active AFTER the projection was written
        await conn.execute(entities.update().where(entities.c.id == b).values(status="rejected"))
        await conn.commit()

        graph, repo = await _bound(conn, session, project)
        response = await graph_query(
            graph,
            repo,
            _POLICY,
            GraphQueryParams(template="neighbors", entity="Acme", hops=2),
            "who is around acme",
            max_graph_hops=3,
        )
        _VALIDATOR.validate(response.to_dict())
        ids = {r.id for r in response.results}
        assert str(b) not in ids  # the drifted entity is gone
        assert str(c) not in ids  # …and so is the node reachable only through it
        assert "PARTIAL_RESULTS" in [w.code for w in response.warnings]  # the drops are surfaced
    finally:
        await _cleanup(session, project)
