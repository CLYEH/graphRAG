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

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import DBAPIError
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
from core.query.results import McpResponse, QueryWarning
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

#: §16 build_id is format:uuid — when the deadline fires DURING scope
#: binding no build was ever resolved; the nil uuid is the honest,
#: format-legal sentinel (the warning message says which case happened).
_NIL_BUILD = "00000000-0000-0000-0000-000000000000"

#: extra outer-bound budget for the hybrid tools: their INTERNAL deadline
#: (same max_latency_ms, started after binding) must win the terminal cut so
#: its partial-results assembly is what the caller sees; the outer bound then
#: only fires on what the inner one cannot see — a binding stall.
_HYBRID_GRACE_MS = 2_000

#: entity_mentions.source_kind → §16 source_type (the C6a mapping).
_MENTION_SOURCE_TYPE = {"text": "chunk", "structured": "row"}


async def _bounded(
    runtime: _Runtime,
    tool: str,
    query: str,
    runner: Any,
    grace_ms: int = 0,
) -> dict[str, Any]:
    """Run one single-mode tool under the project's §21 wall-clock deadline.

    The timeout covers the per-call binding TOO — connection acquisition or
    the active-build lookup can itself stall under DB/network pressure, and a
    §21 deadline that starts after binding would let the call overrun before
    the typed degradation (this is the same rule for every tool; hybrid keeps
    its own richer internal deadline for the mode loop). A timeout is the
    typed §22 degradation, never a hung call or an unhandled cancellation;
    the sql reader's phase transactions roll back under the single
    cancellation (finally runs), and the per-call connection closes with the
    context manager either way."""
    bound_build: str | None = None
    try:
        async with asyncio.timeout((runtime.policy.max_latency_ms + grace_ms) / 1000.0):
            async with runtime.context.bound() as deps:
                bound_build = str(deps.repo.build_id)
                response: McpResponse = await runner(deps)
                return response.to_dict()
    except TimeoutError:
        detail = "" if bound_build else " during scope binding"
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            # binding may itself be what stalled — then no build was ever
            # resolved and the nil uuid marks that honestly (format-legal)
            build_id=bound_build or _NIL_BUILD,
            results=(),
            warnings=(
                QueryWarning(
                    "PARTIAL_RESULTS",
                    f"query exceeded the {runtime.policy.max_latency_ms}ms deadline{detail} (§21)",
                ),
            ),
        ).to_dict()


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """Postgres cancels a statement past ``statement_timeout`` with sqlstate
    57014 (query_canceled) — the DB-side face of the §21 deadline."""
    return getattr(exc.orig, "sqlstate", None) == "57014"


def _introspection_timeout(runtime: _Runtime, build_id: str | None, subject: str) -> dict[str, Any]:
    """The introspection tools' §22 timeout shape (they are not §16 responses,
    so the degradation is an explicit error field, never a hung call). A None
    build_id means the deadline fired during scope binding — nil-uuid
    sentinel, same convention as the §16 tools."""
    detail = "" if build_id else " during scope binding"
    return {
        "project": runtime.context.project,
        "build_id": build_id or _NIL_BUILD,
        "subject": subject,
        "error": f"query exceeded the {runtime.policy.max_latency_ms}ms deadline{detail} (§21)",
    }


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

        async def _run(deps: Any) -> McpResponse:
            return await run_semantic(
                deps.repo, deps.vectors, deps.embedder, query, rt.policy.top_k(top_k)
            )

        return await _bounded(rt, "semantic_search", query, _run)

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
        label = query or f"{template}({entity})"

        async def _run(deps: Any) -> McpResponse:
            return await run_graph(
                deps.graph,
                deps.repo,
                rt.policy.cypher_policy(),
                params,
                label,
                rt.policy.max_graph_hops,
            )

        return await _bounded(rt, "graph_query", label, _run)

    @server.tool()
    async def sql_query(query: str) -> dict[str, Any]:
        """Precise filters/lookups over structured rows (§8 sql, guarded NL→SQL)."""
        rt = _rt()

        async def _run(deps: Any) -> McpResponse:
            return await run_sql(
                deps.sql_reader, deps.llm, rt.policy.sql_policy(), query, rt.policy.sql_rows()
            )

        return await _bounded(rt, "sql_query", query, _run)

    @server.tool()
    async def global_summary(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Corpus-wide community summaries (§8 global)."""
        rt = _rt()

        async def _run(deps: Any) -> McpResponse:
            return await run_global(deps.repo, query, rt.policy.top_k(top_k))

        return await _bounded(rt, "global_summary", query, _run)

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

        async def _run(deps: Any) -> McpResponse:
            return await run_hybrid(deps, hybrid_policy(rt.policy, top_k), query, params)

        # hybrid's INTERNAL deadline paces the mode loop and assembles
        # partial results; the outer bound exists for what it cannot see (a
        # stall during scope binding) and gets a GRACE so the inner deadline
        # always wins the terminal cut — without it the outer timer (started
        # before binding) would fire first and discard completed modes
        return await _bounded(rt, "hybrid_query", query, _run, grace_ms=_HYBRID_GRACE_MS)

    @server.tool()
    async def get_entity(name: str) -> dict[str, Any]:
        """Look one entity up by canonical name (active entities only; the
        SoR decides what an entity IS). Introspection shape — NOT a §16
        response (the frozen tool enum covers only the five retrieval tools)
        — but each entity still carries its §27.2-spirit mention citations."""
        rt = _rt()
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _get_entity(deps.repo, rt.context.project, name)
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, name)

    @server.tool()
    async def list_schema() -> dict[str, Any]:
        """The queryable structured surface: each whitelisted sql table with
        the columns it actually has in the ACTIVE build (empty when the sql
        mode is disabled). Deliberately NOT a §16 response — there is no
        retrieval result to cite; this is introspection."""
        return await _list_schema(_rt())

    @server.tool()
    async def explain_retrieval(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Run the hybrid router and return the response WITH its routing
        trace — the §16 debug block. Gated by the same policy flag as every
        debug emission (§21): when expose_debug is off, the query still runs
        but the trace stays null and a typed warning says why."""
        rt = _rt()

        async def _run(deps: Any) -> McpResponse:
            return await run_hybrid(deps, hybrid_policy(rt.policy, top_k), query, None)

        payload = await _bounded(rt, "hybrid_query", query, _run, grace_ms=_HYBRID_GRACE_MS)
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


async def _list_schema(runtime: _Runtime) -> dict[str, Any]:
    """§9 ``list_schema``: the whitelisted sql tables with their live columns
    (introspection shape). The wall clock covers binding + discovery; the
    STATEMENT deadline fires as a DB error (sqlstate 57014), not
    asyncio.TimeoutError — sql_query already maps this (§22) — and any other
    DB failure degrades with its class named rather than erroring the MCP
    call. A non-DB bug still propagates loud (never laundered as §22)."""
    bound_build: str | None = None
    try:
        async with asyncio.timeout(runtime.policy.max_latency_ms / 1000.0):
            async with runtime.context.bound() as deps:
                bound_build = str(deps.repo.build_id)
                tables: dict[str, list[str]] = {}
                if runtime.policy.text_to_sql.enabled:
                    # the same JSON-key discovery sql_query runs — under the
                    # same reconciled statement deadline (§21), plus the
                    # wall-clock bound around the whole call
                    async with deps.sql_reader.timed_transaction(
                        runtime.policy.sql_policy().timeout_ms
                    ):
                        columns = await deps.sql_reader.columns_by_table(
                            list(runtime.policy.text_to_sql.allowed_tables)
                        )
                    tables = {table: list(cols) for table, cols in columns.items()}
                return {
                    "project": runtime.context.project,
                    # the build these columns belong to — an activation
                    # between this lookup and a later sql_query would
                    # otherwise be undetectable by the caller (DR-001)
                    "build_id": bound_build,
                    "sql_enabled": runtime.policy.text_to_sql.enabled,
                    "tables": tables,
                }
    except TimeoutError:
        return _introspection_timeout(runtime, bound_build, "list_schema")
    except DBAPIError as exc:
        if _is_statement_timeout(exc):
            return _introspection_timeout(runtime, bound_build, "list_schema")
        return {
            "project": runtime.context.project,
            "build_id": bound_build or _NIL_BUILD,
            "subject": "list_schema",
            "error": f"schema discovery failed ({type(exc).__name__}) — §22",
        }


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
