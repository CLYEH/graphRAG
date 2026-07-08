"""Why: BA2c-2b is the first end-to-end wiring of the C1–C11 engine into the
orchestrator — ``default_stages`` closes the six real §5 stages over their deps,
and the orchestrator's control flow (unit-proven with fakes in
test_builds_orchestrator_integration.py) now drives REAL ingest→…→summarize
against live Postgres + Qdrant + Neo4j. Only a live run proves the adapters wire
correctly: each builds its writer/projectors off the handed-in conn, re-reads
from the SoR, and maps its report into the §18 StageResult the orchestrator
records. A deterministic fake LLM/embedder keeps it reproducible with no key.

Two arcs: a structured-only corpus runs all six stages to ``ready`` and lands
rows in every store; and the graph stage's config-gap guard fails a build whose
config declares no ontology while text documents were ingested.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import load_build_config
from core.builds.orchestrator import run_build
from core.builds.stages import default_stages
from core.config import get_settings
from core.registry import add_source, create_job, create_project, get_job
from core.stores import tables
from core.stores.graph import graph_driver
from core.stores.vectors import collection_for, vector_client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_WIPE_PROJECT = "MATCH (n:Entity {project: $project}) DETACH DELETE n"

_STRUCTURED_CONFIG = {
    "structured_mappings": {
        "people": {
            "entities": {
                "person": {"entity_type": "Person", "name_column": "name"},
                "company": {"entity_type": "Company", "name_column": "company"},
            },
            "relations": [{"relation_type": "WORKS_AT", "src": "person", "dst": "company"}],
        }
    }
}


class _FakeLLM:
    """Deterministic community summary — the only LLM call a structured-only
    build makes (graph skips the LLM when config has no ontology)."""

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        answer = json.dumps({"title": "Cluster", "summary": "They work together.", "rating": 5})
        return SimpleNamespace(message=SimpleNamespace(content=answer))


class _FakeEmbedder:
    """Deterministic 4-dim vectors so projection is real without an OpenAI key."""

    async def aget_text_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0, 0.0]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture()
async def stores(migrated: None) -> AsyncIterator[tuple[AsyncQdrantClient, AsyncSession]]:
    client = vector_client()
    driver = graph_driver()
    async with driver.session() as session:
        yield client, session
    await client.close()
    await driver.close()


def _stages_for(
    config_raw: dict[str, Any], client: AsyncQdrantClient, session: AsyncSession
) -> Any:
    return default_stages(
        load_build_config(config_raw),
        chat_model=cast(LLM, _FakeLLM()),
        embedder=cast(BaseEmbedding, _FakeEmbedder()),
        vector_client=client,
        graph_session=session,
    )


async def _count(engine: AsyncEngine, table: sa.Table, project: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.project == project)
            )
        ).scalar_one()


async def _cleanup(
    engine: AsyncEngine, client: AsyncQdrantClient, session: AsyncSession, project: str
) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    await (await session.run(_WIPE_PROJECT, {"project": project})).consume()
    async with engine.connect() as conn, conn.begin():
        step_ids = (
            sa.select(tables.pipeline_steps.c.id)
            .join(tables.pipeline_runs, tables.pipeline_steps.c.run_id == tables.pipeline_runs.c.id)
            .where(tables.pipeline_runs.c.project == project)
        )
        await conn.execute(
            tables.pipeline_step_items.delete().where(
                tables.pipeline_step_items.c.step_id.in_(step_ids)
            )
        )
        await conn.execute(
            tables.pipeline_steps.delete().where(
                tables.pipeline_steps.c.run_id.in_(
                    sa.select(tables.pipeline_runs.c.id).where(
                        tables.pipeline_runs.c.project == project
                    )
                )
            )
        )
        for table in (
            tables.pipeline_runs,
            tables.community_reports,
            tables.entities,  # cascades to relations/mentions/evidence/merge_candidates
            tables.documents,  # cascades to chunks
            tables.jobs,
            tables.sources,
            tables.builds,
        ):
            await conn.execute(table.delete().where(table.c.project == project))
        await conn.execute(tables.projects.delete().where(tables.projects.c.name == project))


async def _write_csv(tmp_path: Path) -> Path:
    csv = tmp_path / "people.csv"
    csv.write_text("id,name,company\n1,Alice,Acme\n2,Bob,Acme\n", encoding="utf-8")
    return csv


async def test_structured_build_runs_all_six_stages_to_ready_across_all_stores(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        csv = await _write_csv(tmp_path)
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            await add_source(
                conn,
                project,
                uri=csv.as_uri(),
                kind="structured",
                metadata={"table": "people", "pk_column": "id"},
            )
            job = await create_job(conn, project, "build")

        outcome = await run_build(
            engine, project, job.id, _stages_for(_STRUCTURED_CONFIG, client, session)
        )

        assert outcome.status == "ready"
        assert not outcome.cancelled and outcome.error is None

        # every store carries this build's output: 2 rows → 2 docs, 3 entities
        # (Alice, Bob, Acme) joined by 2 WORKS_AT, and ≥1 community summary.
        assert await _count(engine, tables.documents, project) == 2
        assert await _count(engine, tables.entities, project) == 3
        assert await _count(engine, tables.relations, project) == 2
        assert await _count(engine, tables.community_reports, project) >= 1

        # Qdrant: the collection exists with the build's points; Neo4j: 3 nodes.
        assert await client.collection_exists(collection_for(project))
        record = await (
            await session.run("MATCH (n:Entity {project: $p}) RETURN count(n) AS c", {"p": project})
        ).single()
        assert record is not None and record["c"] == 3

        async with engine.connect() as conn:
            done_job = await get_job(conn, job.id)
        assert done_job is not None and done_job.status == "done" and done_job.progress == 1.0
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_text_source_without_ontology_fails_the_build_at_graph(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """A build that ingested text documents but whose config declares no
    ontology is a config gap the graph stage refuses (OntologyRequiredError) —
    the build fails at graph rather than silently extracting nothing."""
    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        (tmp_path / "note.txt").write_text("Acme partners with Globex.", encoding="utf-8")
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            await add_source(conn, project, uri=tmp_path.as_uri(), kind="text", metadata={})
            job = await create_job(conn, project, "build")

        outcome = await run_build(engine, project, job.id, _stages_for({}, client, session))

        assert outcome.status == "failed"
        assert not outcome.cancelled
        assert outcome.error is not None and "graph:" in outcome.error
        assert "ontology" in outcome.error
        # ingest + clean ran before the gap; the text document is present.
        assert await _count(engine, tables.documents, project) == 1
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()
