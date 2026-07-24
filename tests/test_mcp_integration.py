"""Why: the MCP server's value is the WIRING — per-call binding to the active
build across all four stores (in the loaded-clean mint order), picked-up
activations between calls, and the get_entity introspection flowing through
live stores. The mode internals are proven in their own suites; this proves
the seam.
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
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.mcp.context import ProjectContext
from core.mcp.server import _get_chunk, _get_document, _get_entity
from core.metadata.schema import MetadataExposure
from core.resolve import fingerprints
from core.stores.graph import graph_driver
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, chunks, documents, entities
from core.stores.tables import projects as projects_table
from core.stores.vectors import vector_client
from tests.conftest import DEMO_QUERY_POLICY, ensure_project

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


class _FakeLLM:
    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(message=SimpleNamespace(content="{}"))


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def context(migrated: None) -> AsyncIterator[ProjectContext]:
    project = f"mcptest-{uuid.uuid4().hex[:10]}"
    ctx = ProjectContext(
        project=project,
        engine=_engine(),
        qdrant=vector_client(),
        neo4j=graph_driver(),
        embedder=cast(BaseEmbedding, _FakeEmbedder()),
        llm=cast(LLM, _FakeLLM()),
    )
    yield ctx
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(entities.delete().where(entities.c.project == project))
        # chunks carry no project column — scope the sweep through the builds
        project_builds = sa.select(builds.c.id).where(builds.c.project == project)
        await conn.execute(chunks.delete().where(chunks.c.build_id.in_(project_builds)))
        await conn.execute(documents.delete().where(documents.c.project == project))
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()
    await ctx.aclose()


async def _activate_build(project: str, *, entity_name: str) -> uuid.UUID:
    engine = _engine()
    async with engine.connect() as conn:
        await ensure_project(conn, project)
        # CFG1: the registry is the ONE policy SoR — the live MCP session
        # reads projects.config, so the seed writes the policy THERE (the
        # exact read path production sessions take; no config.yaml anywhere)
        await conn.execute(
            projects_table.update()
            .where(projects_table.c.name == project)
            .values(config={"query_policy": DEMO_QUERY_POLICY})
        )
        build_id: uuid.UUID = (
            await conn.execute(
                builds.insert().values(project=project, status="building").returning(builds.c.id)
            )
        ).scalar_one()
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        entity_id = uuid.uuid4()
        await writer.insert(
            entities,
            id=entity_id,
            type="org",
            canonical_name=entity_name,
            entity_key=fingerprints.entity_key("org", entity_name),
            status="active",
            review_status="unreviewed",
            created_by="rule",
            created_at=NOW,
            updated_at=NOW,
        )
        await writer.insert_entity_mention(
            entity_id=entity_id,
            source_kind="text",
            source_ref=f"chunk-{entity_id}",
            surface_form=entity_name,
            confidence=1.0,
        )
        await conn.commit()
        # archive any previous active build, then activate this one
        await conn.execute(
            builds.update()
            .where(builds.c.project == project, builds.c.status == "active")
            .values(status="archived")
        )
        await conn.execute(builds.update().where(builds.c.id == build_id).values(status="active"))
        await conn.commit()
    await engine.dispose()
    return build_id


async def test_bound_stores_agree_and_follow_activation(context: ProjectContext) -> None:
    """Per-call binding: every store binds the SAME active build (DR-006 —
    the sql reader's loaned-clean mint order holds on a real connection), and
    a NEW activation between calls is picked up (DR-001: re-resolved per
    call, never cached across calls)."""
    first_build = await _activate_build(context.project, entity_name="Acme")
    async with context.bound() as deps:
        scopes = {
            (deps.repo.project, deps.repo.build_id),
            (deps.vectors.project, deps.vectors.build_id),
            (deps.sql_reader.project, deps.sql_reader.build_id),
            (deps.graph.project, deps.graph.build_id),
        }
        assert scopes == {(context.project, first_build)}

    second_build = await _activate_build(context.project, entity_name="Acme")
    assert second_build != first_build
    async with context.bound() as deps:
        assert deps.repo.build_id == second_build  # activation picked up


async def test_get_entity_returns_cited_entities_from_the_active_build(
    context: ProjectContext,
) -> None:
    await _activate_build(context.project, entity_name="Acme")
    async with context.bound() as deps:
        payload = await _get_entity(deps.repo, context.project, "Acme")
    # introspection shape (NOT §16 — the frozen tool enum covers only the
    # five retrieval tools), but the mention citations still ride along
    assert payload["project"] == context.project
    assert len(payload["entities"]) == 1
    entity = payload["entities"][0]
    assert entity["mentions"][0]["source_type"] == "chunk"  # §27.2's spirit

    async with context.bound() as deps:
        empty = await _get_entity(deps.repo, context.project, "Nobody")
    assert empty["entities"] == []


async def test_a_registered_tool_calls_through_on_live_stores(
    context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One §9 tool exercised through FastMCP's own dispatch: build_server →
    lifespan (engines) → call_tool → per-call binding → live answer. Uses
    list_schema (no LLM, sql disabled in the demo config → an honest empty
    introspection), proving the registration/wiring seam end-to-end. The
    model FACTORIES are faked — the real ones demand an API key the CI
    runner deliberately does not have (§3: provider-blind; the seam under
    test is dispatch/binding, not the vendor client)."""
    import core.mcp.server as server_module
    from core.mcp.server import build_server

    monkeypatch.setattr(server_module, "chat_model", lambda: cast(LLM, _FakeLLM()))
    monkeypatch.setattr(
        server_module, "embedding_model", lambda: cast(BaseEmbedding, _FakeEmbedder())
    )
    await _activate_build(context.project, entity_name="Acme")
    server = build_server(context.project)
    # dispatch through a REAL in-memory protocol session: Server.run enters
    # the lifespan per session and parks the runtime on the session's request
    # context (Codex #58 P1 made the runtime session-scoped, so tools are only
    # callable inside a session — which is also the more protocol-true seam)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as session:
        result = await session.call_tool("list_schema", {})
    unwrapped: Any = result.structuredContent
    if unwrapped is None:
        unwrapped = json.loads(result.content[0].text)  # type: ignore[union-attr]
    if isinstance(unwrapped, dict) and "result" in unwrapped:
        unwrapped = unwrapped["result"]
    assert unwrapped["project"] == context.project
    assert unwrapped["sql_enabled"] is False and unwrapped["tables"] == {}
    # the schema snapshot names its build — a later activation is detectable
    assert uuid.UUID(unwrapped["build_id"])  # real, parseable, never nil


async def _activate_build_with_content(project: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Like ``_activate_build`` but the build also carries one document and
    one chunk — the content ``get_chunk``/``get_document`` exist to serve.
    Returns ``(build_id, chunk_id, document_id)``."""
    engine = _engine()
    async with engine.connect() as conn:
        await ensure_project(conn, project)
        await conn.execute(
            projects_table.update()
            .where(projects_table.c.name == project)
            .values(config={"query_policy": DEMO_QUERY_POLICY})
        )
        build_id: uuid.UUID = (
            await conn.execute(
                builds.insert().values(project=project, status="building").returning(builds.c.id)
            )
        ).scalar_one()
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
        await writer.insert(
            documents,
            id=document_id,
            source_uri="file:///guide.md",
            raw="# 導覽手冊 全票 200 元,優待票 100 元。",
            content_hash=f"hash-{document_id.hex[:12]}",
            mime="text/markdown",
            # a field NO allowlist names — the live DR-010 proof reads it back
            metadata={"governance": {"classification": "secret"}},
            ingested_at=NOW,
        )
        await writer.insert(
            chunks,
            id=chunk_id,
            document_id=document_id,
            ordinal=0,
            text="全票 200 元,優待票 100 元。",
            token_count=12,
            start_offset=7,
            end_offset=22,
        )
        await conn.commit()
        await conn.execute(
            builds.update()
            .where(builds.c.project == project, builds.c.status == "active")
            .values(status="archived")
        )
        await conn.execute(builds.update().where(builds.c.id == build_id).values(status="active"))
        await conn.commit()
    await engine.dispose()
    return build_id, chunk_id, document_id


async def test_a_chunk_uuid_is_exchangeable_for_its_text(context: ProjectContext) -> None:
    """MCP5's reason to exist: a relation evidence ref / chunk result id IS a
    chunk UUID, and before get_chunk nothing on the MCP surface could turn
    one into the text it cites — citations were decoration. Proven against
    the live SoR through the same build-scoped repo production binds."""
    _, chunk_id, document_id = await _activate_build_with_content(context.project)
    async with context.bound() as deps:
        payload = await _get_chunk(deps.repo, context.project, str(chunk_id))
    assert payload["error"] is None
    assert payload["chunk"]["text"] == "全票 200 元,優待票 100 元。"
    assert payload["chunk"]["document_id"] == str(document_id)

    async with context.bound() as deps:
        document = await _get_document(
            deps.repo, context.project, str(document_id), MetadataExposure(fields=())
        )
    assert document["error"] is None
    assert document["document"]["source_uri"] == "file:///guide.md"
    assert "全票 200 元" in document["document"]["raw"]  # the full raw rides along
    # DR-010 live: the stored governance field exists in the SoR row but the
    # empty (default) allowlist keeps it agent-invisible — fail-closed
    assert document["document"]["metadata"] == {}


async def test_an_archived_builds_chunk_is_invisible_to_the_active_binding(
    context: ProjectContext,
) -> None:
    """DR-006: the repo injects build_id=active — a chunk of a superseded
    build must read as not-found, never leak (the whole never-mix-versions
    guarantee, applied to the new introspection read path)."""
    _, old_chunk_id, _ = await _activate_build_with_content(context.project)
    await _activate_build(context.project, entity_name="Acme")  # supersedes
    async with context.bound() as deps:
        payload = await _get_chunk(deps.repo, context.project, str(old_chunk_id))
    assert payload["chunk"] is None
    assert "ACTIVE build" in payload["error"]
