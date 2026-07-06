"""Per-project MCP server factory (§9, C8).

Builds one FastMCP server exposing the frozen §9 tool set over ONE project:
the five retrieval tools (``semantic_search`` · ``graph_query`` ·
``global_summary`` · ``sql_query`` · ``hybrid_query`` — the default entry)
return the frozen §16 contract as-is (``McpResponse.to_dict()``); the three
auxiliary tools (``get_entity`` · ``list_schema`` · ``explain_retrieval``)
are §9-named conveniences: the §16 contract's ``tool`` enum freezes exactly
the five retrieval tools, so ``get_entity``/``list_schema`` return plain
INTROSPECTION shapes (not §16 — claiming the contract would violate its own
enum), while ``explain_retrieval`` returns the hybrid §16 response verbatim
(tool ``hybrid_query``) plus the trace gating below.

Every call re-binds to the ACTIVE build (DR-001, via
:meth:`~core.mcp.context.ProjectContext.bound` — activation between calls is
picked up; no store client is ever touched directly, DR-006). The query
policy is loaded and contract-validated ONCE at startup (fail loud —
:class:`~core.mcp.policy.PolicyError`); ceilings are caller-reconciled here
(the C6b contract) before any mode function sees them. Tool arguments arrive
from an UNTRUSTED agent: the transport layer type-checks them
(FastMCP/pydantic), and the mode functions re-validate at their own doors
(the C6c/C6d lesson) — belt and braces, typed degradation either way.

Transport is stdio (§9 marks transport 🔧; http can be added without touching
the tools). Entry point: ``projects/<name>/mcp_entrypoint.py`` calls
:func:`build_server` and ``server.run()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.mcp.context import ProjectContext
from core.mcp.policy import QueryPolicy, hybrid_policy, load_query_policy
from core.query.global_reports import global_summary as run_global
from core.query.graph import GraphQueryParams
from core.query.graph import graph_query as run_graph
from core.query.hybrid import hybrid_query as run_hybrid
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

#: entity_mentions.source_kind → §16 source_type (the C6a mapping).
_MENTION_SOURCE_TYPE = {"text": "chunk", "structured": "row"}


@dataclass
class _Runtime:
    """The lifespan-held state: one context + one validated policy."""

    context: ProjectContext
    policy: QueryPolicy


def build_server(project: str, config_path: Path) -> FastMCP:
    """One project's MCP server, policy-validated at build time (fail loud)."""
    policy = load_query_policy(config_path)

    runtime: dict[str, _Runtime] = {}

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        settings = get_settings()
        context = ProjectContext(
            project=project,
            engine=create_async_engine(
                settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
                poolclass=NullPool,
            ),
            qdrant=vector_client(),
            neo4j=graph_driver(),
            embedder=embedding_model(),
            llm=chat_model(),
        )
        runtime["current"] = _Runtime(context=context, policy=policy)
        try:
            yield
        finally:
            await context.aclose()

    server = FastMCP(f"graphrag-{project}", lifespan=lifespan)

    def _rt() -> _Runtime:
        return runtime["current"]

    @server.tool()
    async def semantic_search(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Fuzzy/topical retrieval over document text (§8 semantic)."""
        rt = _rt()
        async with rt.context.bound() as deps:
            response = await run_semantic(
                deps.repo, deps.vectors, deps.embedder, query, rt.policy.top_k(top_k)
            )
            return response.to_dict()

    @server.tool()
    async def graph_query(
        template: str,
        entity: str,
        other_entity: str | None = None,
        hops: int = 1,
        query: str = "",
    ) -> dict[str, Any]:
        """Entity-relationship retrieval via the §27.6 parameterized templates
        (neighbors / path / subgraph)."""
        rt = _rt()
        params = GraphQueryParams(
            template=template, entity=entity, other_entity=other_entity, hops=hops
        )
        async with rt.context.bound() as deps:
            response = await run_graph(
                deps.graph,
                deps.repo,
                rt.policy.cypher_policy(),
                params,
                query or f"{template}({entity})",
                rt.policy.max_graph_hops,
            )
            return response.to_dict()

    @server.tool()
    async def sql_query(query: str) -> dict[str, Any]:
        """Precise filters/lookups over structured rows (§8 sql, guarded NL→SQL)."""
        rt = _rt()
        async with rt.context.bound() as deps:
            response = await run_sql(
                deps.sql_reader, deps.llm, rt.policy.sql_policy(), query, rt.policy.sql_rows()
            )
            return response.to_dict()

    @server.tool()
    async def global_summary(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Corpus-wide community summaries (§8 global)."""
        rt = _rt()
        async with rt.context.bound() as deps:
            response = await run_global(deps.repo, query, rt.policy.top_k(top_k))
            return response.to_dict()

    @server.tool()
    async def hybrid_query(
        query: str,
        top_k: int | None = None,
        graph_template: str | None = None,
        graph_entity: str | None = None,
        graph_other_entity: str | None = None,
        graph_hops: int = 1,
    ) -> dict[str, Any]:
        """The default entry (§9): route across every available mode and fuse.
        Supply graph_template + graph_entity to make the graph mode available."""
        rt = _rt()
        params: GraphQueryParams | None = None
        if graph_template is not None and graph_entity is not None:
            params = GraphQueryParams(
                template=graph_template,
                entity=graph_entity,
                other_entity=graph_other_entity,
                hops=graph_hops,
            )
        async with rt.context.bound() as deps:
            response = await run_hybrid(deps, hybrid_policy(rt.policy, top_k), query, params)
            return response.to_dict()

    @server.tool()
    async def get_entity(name: str) -> dict[str, Any]:
        """Look one entity up by canonical name (active entities only; the
        SoR decides what an entity IS). Introspection shape — NOT a §16
        response (the frozen tool enum covers only the five retrieval tools)
        — but each entity still carries its §27.2-spirit mention citations."""
        rt = _rt()
        async with rt.context.bound() as deps:
            return await _get_entity(deps.repo, rt.context.project, name)

    @server.tool()
    async def list_schema() -> dict[str, Any]:
        """The queryable structured surface: each whitelisted sql table with
        the columns it actually has in the ACTIVE build (empty when the sql
        mode is disabled). Deliberately NOT a §16 response — there is no
        retrieval result to cite; this is introspection."""
        rt = _rt()
        async with rt.context.bound() as deps:
            tables: dict[str, list[str]] = {}
            if rt.policy.text_to_sql.enabled:
                # the same JSON-key discovery sql_query runs — under the same
                # reconciled deadline (§21): unbounded, a large structured
                # build could hold this call past the latency cap
                async with deps.sql_reader.timed_transaction(rt.policy.sql_policy().timeout_ms):
                    columns = await deps.sql_reader.columns_by_table(
                        list(rt.policy.text_to_sql.allowed_tables)
                    )
                tables = {table: list(cols) for table, cols in columns.items()}
            return {
                "project": rt.context.project,
                "sql_enabled": rt.policy.text_to_sql.enabled,
                "tables": tables,
            }

    @server.tool()
    async def explain_retrieval(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Run the hybrid router and return the response WITH its routing
        trace — the §16 debug block. Gated by the same policy flag as every
        debug emission (§21): when expose_debug is off, the query still runs
        but the trace stays null and a typed warning says why."""
        rt = _rt()
        async with rt.context.bound() as deps:
            response = await run_hybrid(deps, hybrid_policy(rt.policy, top_k), query, None)
            payload = response.to_dict()
            if not rt.policy.expose_debug:
                payload["warnings"] = [
                    *payload["warnings"],
                    {
                        "code": "GUARDRAIL_BLOCKED",
                        "message": "expose_debug is disabled by policy — no trace emitted (§21)",
                    },
                ]
            return payload

    return server


async def _get_entity(repo: Any, project: str, name: str) -> dict[str, Any]:
    """§9 ``get_entity``: name → the matching ACTIVE entities, each cited by
    its SoR mentions (§27.2's spirit: an entity with zero mentions cannot be
    cited — surfaced as uncited rather than dropped, since this is
    introspection, not a retrieval result)."""
    if not isinstance(name, str) or not name.strip():
        return {
            "project": project,
            "build_id": str(repo.build_id),
            "name": name if isinstance(name, str) else repr(name),
            "error": "name must be a non-blank string",
            "entities": [],
        }
    entity_ids = await repo.entity_ids_by_name(name)
    mentions = await repo.mentions_by_entity(entity_ids)
    return {
        "project": project,
        "build_id": str(repo.build_id),
        "name": name,
        "entities": [
            {
                "id": str(entity_id),
                "mentions": [
                    {"source_type": source_type, "id": source_ref}
                    for kind, source_ref in mentions.get(entity_id, [])
                    if (source_type := _MENTION_SOURCE_TYPE.get(kind)) is not None
                ],
            }
            for entity_id in entity_ids
        ],
    }
