"""Why: BA2c-2b proved the six-stage wiring on a STRUCTURED corpus with a fake
LLM (ontology=None, so the C3b text-extraction path never ran). This is the
first test that drives the TEXT path through ``default_stages`` end to end — a
text document → clean → LLM extraction (C3b) + proposals (C3c) → resolve → index
→ summarize — in two lanes over one tiny corpus:

* **hermetic** (always runs, incl. CI): a context-aware fake LLM returns canned
  extraction/summary JSON, so the full text pipeline is pinned deterministically
  with no key — the first end-to-end proof of the text arc through the orchestrator.
* **real-LLM** (skip-only, key-gated): the SAME pipeline over the real
  ``chat_model()``/``embedding_model()``. It validates the actual model produces
  parseable extractions through ``default_stages`` — the thing a fake cannot. No
  CI secret, so it skips there (owner decision: skip-only, local enforcement —
  it runs on a keyed pre-push `poe check-full`); assertions are lenient because
  real-model output is non-deterministic.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import LLM, ChatMessage, ChatResponse
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import load_build_config
from core.builds.orchestrator import BuildOutcome, run_build
from core.builds.stages import default_stages
from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.registry import add_source, create_job, create_project
from core.stores import tables
from core.stores.graph import graph_driver
from core.stores.vectors import collection_for, vector_client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

# One clear sentence whose entities/relation the ontology names — a real model
# extracts it reliably, and the fake's canned quote is a verbatim substring.
_TEXT = "Alice works at Acme. She joined in 2019 and leads the platform team."
_CONFIG = {"ontology": {"entity_types": ["Person", "Company"], "relation_types": ["WORKS_AT"]}}

_EXTRACTION = json.dumps(
    {
        "entities": [
            {"type": "Person", "name": "Alice", "confidence": 0.9},
            {"type": "Company", "name": "Acme", "confidence": 0.85},
        ],
        "relations": [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Company",
                "dst_name": "Acme",
                "quote": "Alice works at Acme",
                "confidence": 0.8,
            }
        ],
    }
)
_SUMMARY = json.dumps({"title": "Alice & Acme", "summary": "Alice works at Acme.", "rating": 5})


class _FakeLLM:
    """Context-aware: the extraction and summarize steps share one chat model but
    send different system prompts, so a single canned answer can't serve both —
    dispatch on the prompt's opening line."""

    async def achat(self, messages: list[ChatMessage], **_: Any) -> ChatResponse:
        system = str(messages[0].content)
        answer = _SUMMARY if "summarize one community" in system else _EXTRACTION
        return ChatResponse(message=ChatMessage(role="assistant", content=answer))


class _FakeEmbedder:
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


async def _count(engine: AsyncEngine, table: sa.Table, project: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.project == project)
            )
        ).scalar_one()


async def _run_text_build(
    engine: AsyncEngine,
    project: str,
    stores: tuple[AsyncQdrantClient, AsyncSession],
    chat: LLM,
    embed: BaseEmbedding,
    tmp_path: Path,
) -> BuildOutcome:
    client, session = stores
    (tmp_path / "note.txt").write_text(_TEXT, encoding="utf-8")
    async with engine.connect() as conn, conn.begin():
        await create_project(conn, name=project)
        await add_source(conn, project, uri=tmp_path.as_uri(), kind="text", metadata={})
        job = await create_job(conn, project, "build")
    stages = default_stages(
        load_build_config(_CONFIG),
        chat_model=chat,
        embedder=embed,
        vector_client=client,
        graph_session=session,
    )
    return await run_build(engine, project, job.id, stages)


async def _cleanup(
    engine: AsyncEngine, client: AsyncQdrantClient, session: AsyncSession, project: str
) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    await (
        await session.run("MATCH (n:Entity {project: $p}) DETACH DELETE n", {"p": project})
    ).consume()
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
            tables.ontology_proposals,
            tables.review_ledger,  # non-build-scoped (DR-003) — clean by project so a
            # real-lane auto-merge decision doesn't persist globally across runs
            tables.entities,  # cascades to relations/mentions/evidence/merge_candidates
            tables.documents,  # cascades to chunks
            tables.jobs,
            tables.sources,
            tables.builds,
        ):
            await conn.execute(table.delete().where(table.c.project == project))
        await conn.execute(tables.projects.delete().where(tables.projects.c.name == project))


async def test_text_pipeline_hermetic_extracts_and_builds(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """The full text arc through default_stages with a fake LLM — deterministic,
    so it pins exact rows and runs in CI (no key)."""
    engine = _engine()
    project = _proj()
    try:
        outcome = await _run_text_build(
            engine,
            project,
            stores,
            cast(LLM, _FakeLLM()),
            cast(BaseEmbedding, _FakeEmbedder()),
            tmp_path,
        )
        assert outcome.status == "ready" and outcome.error is None
        # C3b: the LLM's Alice (Person) + Acme (Company) and their WORKS_AT edge.
        assert await _count(engine, tables.entities, project) == 2
        assert await _count(engine, tables.relations, project) == 1
        assert await _count(engine, tables.community_reports, project) >= 1
    finally:
        client, session = stores
        await _cleanup(engine, client, session, project)
        await engine.dispose()


@pytest.mark.skipif(
    not get_settings().openai_api_key,
    reason="real-LLM lane needs OPENAI_API_KEY (skip-only, local enforcement)",
)
async def test_text_pipeline_real_llm_extracts_and_builds(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """The SAME pipeline over the REAL model — validates that a live extraction
    parses and lands through default_stages. Lenient (model output varies): the
    build reaches ready, the chunk is embedded, and at least one entity is
    extracted from an unambiguous sentence."""
    engine = _engine()
    project = _proj()
    try:
        outcome = await _run_text_build(
            engine, project, stores, chat_model(), embedding_model(), tmp_path
        )
        assert outcome.status == "ready" and outcome.error is None
        assert await _count(engine, tables.entities, project) >= 1
        client, _ = stores
        collection = await client.get_collection(collection_for(project))
        assert (collection.points_count or 0) >= 1  # the chunk was embedded
    finally:
        client, session = stores
        await _cleanup(engine, client, session, project)
        await engine.dispose()
