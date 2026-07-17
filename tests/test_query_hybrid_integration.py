"""Why: hybrid_query is only correct if REAL bound stores wire through it —
the four repos minted off one active build, the scope re-check, mode fan-out
against live Postgres/Qdrant/Neo4j, fusion, and the debug trace must all hold
end-to-end. Mode internals are proven in their own suites; this is the
integration seam: one question → routed → fused → schema-valid response.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncSession
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.query.hybrid import HybridDeps, HybridPolicy, hybrid_query
from core.query.policy import (
    CYPHER_ALLOWED_CLAUSES,
    CYPHER_BLOCKED_MIN,
    SQL_BLOCKED_KEYWORDS_MIN,
    TextToCypher,
    TextToSql,
)
from core.resolve import fingerprints
from core.stores.graph import BuildScopedGraphProjector, BuildScopedGraphRepo, graph_driver
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.tables import builds, community_reports, entities, relations
from core.stores.vectors import BuildScopedVectorRepo, vector_client
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)

_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


class _FakeEmbedder:
    async def aget_text_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0, 0.0]


class _SelectorLLM:
    """Routes to global + semantic; never asked to write SQL in this test."""

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        answer = json.dumps({"modes": ["semantic", "global"], "reason": "topical"})
        return SimpleNamespace(message=SimpleNamespace(content=answer))


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def stores(migrated: None) -> AsyncIterator[tuple[AsyncConnection, AsyncSession, Any]]:
    engine = _engine()
    driver = graph_driver()
    client = vector_client()
    async with engine.connect() as conn, driver.session() as session:
        yield conn, session, client
    await client.close()
    await driver.close()
    await engine.dispose()


async def _cleanup(project: str) -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.execute(
            community_reports.delete().where(community_reports.c.project == project)
        )
        await connection.execute(entities.delete().where(entities.c.project == project))
        await connection.execute(builds.delete().where(builds.c.project == project))
        await connection.commit()
    await engine.dispose()


async def test_hybrid_routes_fuses_and_traces_on_live_stores(
    stores: tuple[AsyncConnection, AsyncSession, Any],
) -> None:
    conn, session, client = stores
    project = f"hybtest-{uuid.uuid4().hex[:10]}"
    try:
        await ensure_project(conn, project)
        build_id: uuid.UUID = (
            await conn.execute(
                builds.insert().values(project=project, status="building").returning(builds.c.id)
            )
        ).scalar_one()
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        member = uuid.uuid4()
        await writer.insert(
            entities,
            id=member,
            type="org",
            canonical_name="Acme",
            entity_key=fingerprints.entity_key("org", "Acme"),
            status="active",
            review_status="unreviewed",
            created_by="rule",
            created_at=NOW,
            updated_at=NOW,
        )
        await writer.insert(
            community_reports,
            id=uuid.uuid4(),
            level=0,
            title="Acme cluster",
            summary="All about Acme.",
            member_entity_ids=[member],
            rating=7.0,
        )
        await conn.commit()
        await conn.execute(builds.update().where(builds.c.id == build_id).values(status="active"))
        await conn.commit()

        # the sql reader is minted FIRST: its loaned-clean contract (C6b)
        # demands no open transaction, and the other factories' lookups
        # auto-begin one on the shared connection
        sql_reader = await BuildScopedSqlReader.for_active_build(conn, project)
        repo = await BuildScopedRepo.for_active_build(conn, project)
        vectors = await BuildScopedVectorRepo.for_active_build(conn, client, project)
        graph = await BuildScopedGraphRepo.for_active_build(conn, session, project)
        deps = HybridDeps(
            repo=repo,
            vectors=vectors,
            embedder=cast(BaseEmbedding, _FakeEmbedder()),
            sql_reader=sql_reader,
            graph=graph,
            llm=cast(LLM, _SelectorLLM()),
        )
        policy = HybridPolicy(
            text_to_sql=TextToSql(
                enabled=False,  # gated: surfaces as MODE_SKIPPED, never offered
                allowed_tables=(),
                blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
                max_rows=50,
                timeout_ms=1000,
            ),
            text_to_cypher=TextToCypher(
                enabled=False,
                allowed_clauses=CYPHER_ALLOWED_CLAUSES,
                blocked=CYPHER_BLOCKED_MIN,
                max_rows=50,
                timeout_ms=1000,
            ),
            max_graph_hops=3,
            top_k=10,
            max_sql_rows=50,
            expose_debug=True,
        )

        response = await hybrid_query(deps, policy, "what is acme about")
        _VALIDATOR.validate(response.to_dict())

        # the global report came through fusion, cited by its member entity
        report_hits = [r for r in response.results if r.result_type == "community_report"]
        assert len(report_hits) == 1 and report_hits[0].title == "Acme cluster"
        assert report_hits[0].source_refs[0].source_type == "entity"
        assert report_hits[0].source_refs[0].id == str(member)

        # gated modes are surfaced, never silently absent — and the QP1 auto
        # plan changed graph's fate here: "what is acme about" NAMES the
        # build's Acme entity, so the plan links it and graph RUNS (neighbors
        # around Acme over live Neo4j — an unprojected node yields zero hits,
        # but the mode ran and the trace says why), while sql stays gated
        skipped = [w.message for w in response.warnings if w.code == "MODE_SKIPPED"]
        assert any("sql mode skipped" in m for m in skipped)
        assert not any("graph mode skipped" in m for m in skipped)

        # the trace tells the truth about the live run
        assert response.debug is not None
        decision = response.debug["routing_decision"]
        # ordered insert (Codex #89 R1): graph sits at its _MODE_ORDER slot
        assert decision["selected"] == ["semantic", "graph", "global"]
        assert decision["skipped"] == ["sql"]
        assert "auto plan" in response.debug["retrieval_plan"][0]
        assert "Acme" in response.debug["retrieval_plan"][0]
        assert "postgres" in response.debug["stores_used"]
        assert response.debug["latency_ms"] >= 0
    finally:
        await _cleanup(project)


_WIPE_PROJECT = """\
MATCH (n:Entity {project: $project})
DETACH DELETE n
"""


async def test_auto_plan_answers_a_relation_question_semantic_cannot(
    stores: tuple[AsyncConnection, AsyncSession, Any],
) -> None:
    """QP1's golden shape (review §P0#3): a question that NEEDS a relation
    path — asked with NO graph options — must reach the graph mode via the
    auto plan and return the path hit. The pure-semantic baseline holds zero
    relation knowledge (no chunks embed this fact), so the path result is
    strictly beyond it: hybrid-without-options now answers what semantic
    alone structurally cannot."""
    conn, session, client = stores
    project = f"hybtest-{uuid.uuid4().hex[:10]}"
    try:
        await ensure_project(conn, project)
        build_id: uuid.UUID = (
            await conn.execute(
                builds.insert().values(project=project, status="building").returning(builds.c.id)
            )
        ).scalar_one()
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)

        async def seed_entity(name: str) -> tuple[uuid.UUID, str]:
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
            return entity_id, key

        acme = await seed_entity("Acme")
        bobco = await seed_entity("BobCo")
        signature = fingerprints.relation_signature(acme[1], "works_with", bobco[1])
        await writer.insert(
            relations,
            id=uuid.uuid4(),
            src_entity_id=acme[0],
            dst_entity_id=bobco[0],
            type="works_with",
            relation_signature=signature,
            status="active",
            review_status="unreviewed",
            created_by="rule",
            confidence=1.0,
            created_at=NOW,
            updated_at=NOW,
        )
        await conn.commit()
        projector = await BuildScopedGraphProjector.for_building_build(
            conn, session, project, build_id
        )
        for row in await writer.fetch_all(entities):
            await projector.project_entity(
                str(row.id), row.type, row.status, name=row.canonical_name
            )
        for row in await writer.fetch_all(relations):
            await projector.project_relation(
                str(row.src_entity_id), str(row.dst_entity_id), row.type
            )
        await conn.execute(builds.update().where(builds.c.id == build_id).values(status="active"))
        await conn.commit()

        sql_reader = await BuildScopedSqlReader.for_active_build(conn, project)
        repo = await BuildScopedRepo.for_active_build(conn, project)
        vectors = await BuildScopedVectorRepo.for_active_build(conn, client, project)
        graph = await BuildScopedGraphRepo.for_active_build(conn, session, project)
        deps = HybridDeps(
            repo=repo,
            vectors=vectors,
            embedder=cast(BaseEmbedding, _FakeEmbedder()),
            sql_reader=sql_reader,
            graph=graph,
            llm=cast(LLM, _SelectorLLM()),  # picks semantic+global — graph joins by plan
        )
        policy = HybridPolicy(
            text_to_sql=TextToSql(
                enabled=False,
                allowed_tables=(),
                blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
                max_rows=50,
                timeout_ms=1000,
            ),
            text_to_cypher=TextToCypher(
                enabled=False,
                allowed_clauses=CYPHER_ALLOWED_CLAUSES,
                blocked=CYPHER_BLOCKED_MIN,
                max_rows=50,
                timeout_ms=2000,
            ),
            max_graph_hops=3,
            top_k=10,
            max_sql_rows=50,
            expose_debug=True,
        )

        response = await hybrid_query(deps, policy, "Acme 和 BobCo 是什麼關係?")
        _VALIDATOR.validate(response.to_dict())

        # the auto plan read the two names in question order → path template
        assert response.debug is not None
        plan_line = response.debug["retrieval_plan"][0]
        assert "auto plan" in plan_line and "path" in plan_line
        assert "Acme" in plan_line and "BobCo" in plan_line
        assert "graph" in response.debug["routing_decision"]["selected"]

        # the relation chain surfaced — the hit semantic alone cannot produce
        # (nothing was embedded; the fact lives only in the graph)
        path_hits = [r for r in response.results if r.result_type == "path"]
        assert path_hits, f"no path hit in {[r.result_type for r in response.results]}"
        # the path TEXT carries the arrow chain, cited by the relation row
        assert any("works_with" in (hit.text or "") for hit in path_hits)
        assert path_hits[0].source_refs[0].source_type == "relation"
    finally:
        await _cleanup(project)
        await (await session.run(_WIPE_PROJECT, {"project": project})).consume()
