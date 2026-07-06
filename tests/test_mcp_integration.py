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
from alembic import command
from alembic.config import Config
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.mcp.context import ProjectContext
from core.mcp.server import _get_entity
from core.resolve import fingerprints
from core.stores.graph import graph_driver
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, entities
from core.stores.vectors import vector_client

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
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.commit()
    await engine.dispose()
    await ctx.aclose()


async def _activate_build(project: str, *, entity_name: str) -> uuid.UUID:
    engine = _engine()
    async with engine.connect() as conn:
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
    server = build_server(context.project, REPO_ROOT / "projects" / "demo" / "config.yaml")
    lifespan = server.settings.lifespan
    assert lifespan is not None
    async with lifespan(server):
        result = await server.call_tool("list_schema", {})
    # FastMCP wraps returns; unwrap the structured payload
    unwrapped: Any = result[1] if isinstance(result, tuple) else result
    if isinstance(unwrapped, dict) and "result" in unwrapped:
        unwrapped = unwrapped["result"]
    assert unwrapped["project"] == context.project
    assert unwrapped["sql_enabled"] is False and unwrapped["tables"] == {}
    # the schema snapshot names its build — a later activation is detectable
    assert uuid.UUID(unwrapped["build_id"])  # real, parseable, never nil
