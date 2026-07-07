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
from core.stores.graph import BuildScopedGraphRepo, graph_driver
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.tables import builds, community_reports, entities
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

        # gated modes are surfaced, never silently absent
        skipped = [w.message for w in response.warnings if w.code == "MODE_SKIPPED"]
        assert any("sql mode skipped" in m for m in skipped)
        assert any("graph mode skipped" in m for m in skipped)

        # the trace tells the truth about the live run
        assert response.debug is not None
        decision = response.debug["routing_decision"]
        assert decision["selected"] == ["semantic", "global"]
        assert sorted(decision["skipped"]) == ["graph", "sql"]
        assert "postgres" in response.debug["stores_used"]
        assert response.debug["latency_ms"] >= 0
    finally:
        await _cleanup(project)
