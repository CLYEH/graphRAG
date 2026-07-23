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
policy is loaded from the REGISTRY at each session's lifespan start and
contract-validated (fail loud — :class:`~core.mcp.policy.PolicyError`;
CFG1: ``projects.config`` is the one SoR); ceilings are caller-reconciled here
(the C6b contract) before any mode function sees them. Tool arguments arrive
from an UNTRUSTED agent: the transport layer type-checks them
(FastMCP/pydantic), and the mode functions re-validate at their own doors
(the C6c/C6d lesson) — belt and braces, typed degradation either way.

Transports (§9 🔧): stdio (default) and streamable HTTP (C8b — the external
no-code agent platform consumes MCP over HTTP), selected at RUN time via
:func:`run_server`; the tools and policy are transport-agnostic, exactly the
additivity the original stdio-only note promised. HTTP binds
``core.config``'s ``mcp_http_host``/``mcp_http_port`` (localhost by default —
wider exposure is an operator opt-in while §23 auth remains a placeholder).
Entry point: ``graphrag serve-mcp`` (CFG1 gateway — one process, every
project at ``/mcp/<project>``); :func:`build_server` also serves stdio
one-project runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.mcp.context import ProjectContext
from core.mcp.policy import (
    QueryPolicy,
    hybrid_policy,
    load_runtime_config_from_registry,
)
from core.metadata.schema import MetadataExposure
from core.query.global_reports import global_summary as run_global
from core.query.graph import GraphQueryParams
from core.query.graph import graph_query as run_graph
from core.query.hybrid import hybrid_query as run_hybrid
from core.query.metadata_enrich import enrich_response_metadata
from core.query.results import McpResponse, QueryWarning
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.stores.errors import STORE_CLIENT_ERRORS, store_name
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

#: §16 build_id is format:uuid — when the deadline fires DURING scope
#: binding no build was ever resolved; the nil uuid is the honest,
#: format-legal sentinel (the warning message says which case happened).
_NIL_BUILD = "00000000-0000-0000-0000-000000000000"

#: the store CLIENTS' exception families (§22 STORE_UNAVAILABLE) and their
#: store names now live in core.stores.errors — hybrid's per-mode guard uses
#: the same map, so the two degradation surfaces cannot drift (Codex #122 r3).
#: Deliberately NOT Exception either way: an in-code bug still propagates
#: LOUD — degradation is for store trouble, never for our own bugs.
_STORE_ERRORS: tuple[type[BaseException], ...] = STORE_CLIENT_ERRORS
_store_name = store_name


#: entity_mentions.source_kind → §16 source_type (the C6a mapping).
_MENTION_SOURCE_TYPE = {"text": "chunk", "structured": "row"}


async def _bounded(
    runtime: _Runtime,
    tool: str,
    query: str,
    runner: Any,
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
    deadline = time.monotonic() + runtime.policy.max_latency_ms / 1000.0
    try:
        async with asyncio.timeout(runtime.policy.max_latency_ms / 1000.0):
            async with runtime.context.bound() as deps:
                bound_build = str(deps.repo.build_id)
                # the runner gets what binding LEFT of the budget — a pacer
                # inside it (hybrid) starts from the REMAINDER, never a
                # fresh full budget, so the whole call respects the cap and
                # the inner deadline beats this outer one in all but a μs
                # photo finish (either way a typed §22 cut; partial assembly
                # stays with the pacer, and the outer cut covers what no
                # inner timer can see — the binding itself)
                remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
                response: McpResponse = await runner(deps, remaining_ms)
                # enrich chunk source_refs with the exposed slice of their
                # document metadata (DR-010 rule 6/7) — one place for every
                # modality, inside the deadline + the build-scoped binding
                response = await enrich_response_metadata(response, deps.repo, runtime.exposure)
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
    except _STORE_ERRORS as exc:
        # a store outage during binding or the mode run degrades typed
        # (§22 STORE_UNAVAILABLE), never an MCP transport error; hybrid maps
        # per-mode internally — this is the single-mode tools' equivalent
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            build_id=bound_build or _NIL_BUILD,
            results=(),
            warnings=(
                QueryWarning(
                    "STORE_UNAVAILABLE",
                    f"{_store_name(exc)} unavailable ({type(exc).__name__}) — degraded "
                    "to an empty typed response (§22)",
                ),
            ),
        ).to_dict()


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """Postgres cancels a statement past ``statement_timeout`` with sqlstate
    57014 (query_canceled) — the DB-side face of the §21 deadline."""
    return getattr(exc.orig, "sqlstate", None) == "57014"


def _introspection_store_error(
    runtime: _Runtime, build_id: str | None, subject: str, exc: BaseException
) -> dict[str, Any]:
    """The introspection tools' §22 store-outage shape — the same explicit
    error field as the timeout shape, naming the store exception class."""
    return {
        "project": runtime.context.project,
        "build_id": build_id or _NIL_BUILD,
        "subject": subject,
        "error": f"{_store_name(exc)} unavailable ({type(exc).__name__}) — §22",
    }


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
    """The lifespan-held state: one context + one validated policy + the
    document-metadata exposure allowlist (fail-closed empty by default)."""

    context: ProjectContext
    policy: QueryPolicy
    exposure: MetadataExposure = field(default_factory=lambda: MetadataExposure(fields=()))


async def _load_runtime_config(engine: Any, project: str) -> tuple[QueryPolicy, MetadataExposure]:
    """One connection, one registry read (CFG1) — the lifespan's policy seam,
    module-level so hermetic tests can stub the WHOLE acquisition (fake
    engines carry no ``connect``)."""
    async with engine.connect() as conn:
        return await load_runtime_config_from_registry(conn, project)


def build_server(project: str) -> FastMCP:
    """One project's MCP server — policy read from the REGISTRY per session.

    CFG1: ``projects.config`` is the ONE policy SoR (owner 2026-07-17,
    superseding the 2026-07-10 dual-source decision) — no ``config.yaml``.
    The load moved from build time to LIFESPAN start, which the SDK enters
    once per protocol session: a policy edit applies to the NEXT session,
    and a project with a missing/invalid registry policy fails that
    session's startup loud (typed :class:`~core.mcp.policy.PolicyError`),
    never half-serves."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[_Runtime]:
        settings = get_settings()
        engine = create_async_engine(
            settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool,
        )
        # policy BEFORE any store/model client (Codex #93 R5): when the
        # registry policy is missing/invalid AND a client factory would also
        # fail (e.g. no OPENAI_API_KEY), startup must surface the actionable
        # typed PolicyError, not the masking client error. Only the engine
        # exists at this point, so a load failure disposes exactly that.
        try:
            policy, exposure = await _load_runtime_config(engine, project)
        except BaseException:
            await engine.dispose()
            raise
        context = ProjectContext(
            project=project,
            engine=engine,
            qdrant=vector_client(),
            neo4j=graph_driver(),
            embedder=embedding_model(),
            llm=chat_model(),
        )
        try:
            yield _Runtime(context=context, policy=policy, exposure=exposure)
        finally:
            await context.aclose()

    # host/port are read at BUILD time like the policy (a later env change
    # applies on the next build); they only matter for the http transport —
    # stdio ignores them
    http_settings = get_settings()
    server = FastMCP(
        f"graphrag-{project}",
        lifespan=lifespan,
        host=http_settings.mcp_http_host,
        port=http_settings.mcp_http_port,
    )

    def _rt() -> _Runtime:
        # SESSION-scoped, via the SDK's own channel: Server.run enters the
        # lifespan once per protocol session and parks the yielded value on
        # that session's request context — and streamable HTTP multiplexes
        # MANY sessions on one process (Codex #58 P1). A module-level slot
        # here would be overwritten by every later session's startup and
        # would hand tools already-closed store clients once any session
        # ends; the request context always resolves to the CALLING session's
        # own runtime. (stdio = exactly one session; behavior unchanged.)
        rt: _Runtime = server.get_context().request_context.lifespan_context
        return rt

    @server.tool()
    async def semantic_search(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Fuzzy/topical retrieval over document text (§8 semantic)."""
        rt = _rt()

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
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

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
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

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
            return await run_sql(
                deps.sql_reader, deps.llm, rt.policy.sql_policy(), query, rt.policy.sql_rows()
            )

        return await _bounded(rt, "sql_query", query, _run)

    @server.tool()
    async def global_summary(query: str, top_k: int | None = None) -> dict[str, Any]:
        """Corpus-wide community summaries (§8 global)."""
        rt = _rt()

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
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
        Supply graph_template + graph_entity to run YOUR graph invocation;
        without them the router derives a safe plan itself when the question
        names a build entity (QP1 auto plan — see the routing trace)."""
        rt = _rt()
        params: GraphQueryParams | None = None
        if graph_template is not None and graph_entity is not None:
            params = GraphQueryParams(
                template=graph_template,
                entity=graph_entity,
                other_entity=graph_other_entity,
                hops=graph_hops,
            )

        async def _run(deps: Any, remaining_ms: int) -> McpResponse:
            # hybrid's internal pacer runs on what binding LEFT of the §21
            # budget — never a fresh full one — so its earlier deadline wins
            # the terminal cut in all but a μs photo finish (partial
            # assembly) and the whole call stays within max_latency_ms
            return await run_hybrid(
                deps,
                hybrid_policy(rt.policy, top_k, latency_budget_ms=remaining_ms),
                query,
                params,
            )

        return await _bounded(rt, "hybrid_query", query, _run)

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
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, name, exc)

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

        async def _run(deps: Any, remaining_ms: int) -> McpResponse:
            return await run_hybrid(
                deps,
                hybrid_policy(rt.policy, top_k, latency_budget_ms=remaining_ms),
                query,
                None,
            )

        payload = await _bounded(rt, "hybrid_query", query, _run)
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
        return _introspection_store_error(runtime, bound_build, "list_schema", exc)
    except _STORE_ERRORS as exc:
        # binding touches the other stores' clients too (qdrant/neo4j) — the
        # same §22 line as _bounded
        return _introspection_store_error(runtime, bound_build, "list_schema", exc)


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


#: §9's user-facing transport vocabulary → the SDK's transport names. "http"
#: is streamable HTTP (the MCP spec's current HTTP transport); SSE is the
#: SDK's legacy HTTP flavor and deliberately NOT offered — one HTTP transport,
#: no ambiguity for the consuming platform.
TRANSPORTS: Final[dict[str, str]] = {"stdio": "stdio", "http": "streamable-http"}


def run_server(server: FastMCP, transport: str = "stdio") -> None:
    """Run a built server on a §9 transport — the one place the vocabulary is
    mapped, so every project entrypoint offers the same choices. Unknown
    names fail loud (a typo'd transport must never silently fall back to
    stdio and strand the HTTP consumer)."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r} (choose from {sorted(TRANSPORTS)})")
    server.run(transport=cast(Any, TRANSPORTS[transport]))


async def run_bounded_query(
    context: ProjectContext,
    policy: QueryPolicy,
    tool: str,
    query: str,
    runner: Any,
    exposure: MetadataExposure | None = None,
) -> dict[str, Any]:
    """Public seam for non-MCP facades (the Console query playground, BA6):
    the SAME §21 wall-clock deadline + per-call binding + §22 typed
    degradation envelope every MCP tool runs under — one machinery, two
    facades, so the REST playground can never drift from the MCP tools
    (class 5). ``runner(deps, remaining_ms) -> McpResponse`` exactly as the
    tools pass it; the returned dict is the §16 shape (the REST layer
    reprojects it onto the frozen QueryResult). ``exposure`` is the caller's
    document-metadata allowlist (the Console reads it from ``projects.config``);
    None is the fail-closed empty allowlist."""
    runtime = _Runtime(
        context=context,
        policy=policy,
        exposure=exposure or MetadataExposure(fields=()),
    )
    return await _bounded(runtime, tool, query, runner)
