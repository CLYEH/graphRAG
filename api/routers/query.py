"""Query playground endpoints (BA6a semantic/sql/global + BA6b graph/hybrid)
over the ACTIVE build.

Each POST runs its §8 mode through ``core.mcp.server.run_bounded_query`` —
the SAME §21 wall-clock deadline + per-call binding + §22 typed-degradation
envelope every MCP tool uses (one machinery, two facades: the playground can
never answer differently from the agent surface). The per-request
``ProjectContext`` is built off the API's own lazily-held clients
(``api.deps.project_query_context``); the policy comes from the registry
config (the BA3c seam, strict — no invented §21 defaults); and the §16 dict
is reprojected onto the frozen QueryResult (mode/build_id/results/
graph_context/warnings/debug — ``build_id`` keeps the MCP nil-uuid sentinel
when the deadline fires during binding, format-legal and honest).

GAP (registry_errors precedent): an unconfigured LLM/embedding model
(``LLMNotConfiguredError`` — no API key) maps to 503 STORE_UNAVAILABLE with a
message naming the missing configuration: the mode's model dependency is
unavailable, and no frozen code says "server not configured".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError

from api.deps import project_query_context, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.registry_errors import translate_registry_error
from api.schemas import GraphOptions, GraphQueryRequest, HybridQueryRequest, QueryRequest
from core.llm.factory import LLMNotConfiguredError
from core.mcp.policy import PolicyError, QueryPolicy, hybrid_policy, query_policy_from_mapping
from core.mcp.server import run_bounded_query
from core.query.global_reports import global_summary as run_global
from core.query.graph import GraphQueryParams
from core.query.graph import graph_query as run_graph
from core.query.hybrid import hybrid_query as run_hybrid
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.registry import ProjectNotFoundError, get_project
from core.stores.repo import NoActiveBuildError, resolve_active_binding

router = APIRouter(tags=["query"])

_NIL_BUILD = "00000000-0000-0000-0000-000000000000"


async def _load_policy(request: Request, project: str) -> QueryPolicy:
    """Project 404 first, active build 409 second, THEN the registry-config
    policy (the BA3c seam) — the inspect ``_bind`` precedence (Codex #60 R3):
    a bootstrap project with no active build must hear the frozen
    NO_ACTIVE_BUILD, not 400/503 config errors pointing at the wrong lever.
    The precheck is error-precedence only, not the binding of record — the
    seam still re-binds per call under the §21 deadline, and a race to
    deactivation still maps to 409 in ``_run_mode``.

    The reads run on a SHORT-LIVED connection, returned to the pool before
    the bounded query begins — a request must never hold one pool connection
    while waiting to acquire another from the SAME pool (Codex #60 R2, P1):
    at pool capacity every worker would sit on its policy connection waiting
    for a binding connection, and healthy queries would burn their §21
    deadline in the convoy. The query endpoints therefore take NO ``Conn``
    yield-dep at all (which lives until the response completes — the BA2e-2
    lesson's pool-shaped sibling)."""
    try:
        async with request.app.state.engine.connect() as conn:
            proj = await get_project(conn, project)
            if proj is None:
                raise translate_registry_error(ProjectNotFoundError(project))
            try:
                await resolve_active_binding(conn, project)
            except NoActiveBuildError as exc:
                raise translate_registry_error(exc) from exc
    except SQLAlchemyError as exc:
        # the preflight runs BEFORE the seam's §22 store-degradation path, so
        # a Postgres/pool outage here must map to the typed 503 itself — the
        # inspect Neo4j precedent: an outage is 503 STORE_UNAVAILABLE, never
        # the generic INTERNAL 500 server-bug envelope (Codex #60 R4)
        raise ApiError(
            ErrorCode.STORE_UNAVAILABLE,
            "registry store unavailable while resolving the project policy",
        ) from exc
    block = (proj.config or {}).get("query_policy")
    if block is None:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"project {project!r} has no query_policy configured — "
            "PATCH the project config with a query_policy block (§21)",
            details={"query_policy": "missing"},
        )
    try:
        return query_policy_from_mapping(block)
    except PolicyError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, str(exc), details={"query_policy": "invalid"}
        ) from exc


def _graph_params(options: GraphOptions) -> GraphQueryParams:
    """The typed options → the §27.6 invocation, field for field."""
    return GraphQueryParams(
        template=options.template,
        entity=options.entity,
        other_entity=options.other_entity,
        hops=options.hops,
    )


async def _run_mode(
    request: Request,
    project: str,
    mode: str,
    query: str,
    runner: Any,
) -> dict[str, Any]:
    policy = await _load_policy(request, project)
    try:
        context = project_query_context(request, project)
    except LLMNotConfiguredError as exc:
        # GAP (module docstring): the mode's model dependency is unavailable —
        # a typed 503 naming the missing config, never a coarse 500
        raise ApiError(ErrorCode.STORE_UNAVAILABLE, str(exc)) from exc
    try:
        mcp_dict = await run_bounded_query(context, policy, f"query_{mode}", query, runner(policy))
    except NoActiveBuildError as exc:
        raise translate_registry_error(exc) from exc
    build_id = mcp_dict.get("build_id")
    payload = {
        "mode": mode,
        "build_id": build_id,
        "results": mcp_dict.get("results", []),
        "graph_context": mcp_dict.get("graph_context"),
        "warnings": mcp_dict.get("warnings", []),
        "debug": mcp_dict.get("debug"),
    }
    return success(
        payload,
        **response_meta(request),
        # meta names the serving build only when one was actually bound — the
        # nil sentinel stays in data.build_id (required, format-legal) but is
        # not a real build for meta's nullable field
        build_id=None if build_id in (None, _NIL_BUILD) else build_id,
    )


@router.post("/projects/{project}/query/semantic")
async def query_semantic_endpoint(
    request: Request, project: str, body: QueryRequest
) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_semantic(
                deps.repo, deps.vectors, deps.embedder, body.query, policy.top_k(body.top_k)
            )

        return _run

    return await _run_mode(request, project, "semantic", body.query, runner)


@router.post("/projects/{project}/query/sql")
async def query_sql_endpoint(request: Request, project: str, body: QueryRequest) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            # the caller's top_k NARROWS the §21 sql row ceiling (min, never
            # widens — the BA3c limit precedent); accepting-and-ignoring it
            # would silently exceed the requested cap (Codex #60 R1). The
            # frozen policy schema defines max_top_k as the upper bound on
            # QueryRequest.top_k, so the request cap clamps through
            # policy.top_k() FIRST, then meets the row ceiling (Codex #60
            # R4); with no top_k the mode's own ceiling stands. MCP's sql
            # tool exposes no per-call cap, so this is REST-additive on top
            # of the SAME shared envelope, not a facade divergence.
            ceiling = policy.sql_rows()
            rows = min(policy.top_k(body.top_k), ceiling) if body.top_k is not None else ceiling
            return await run_sql(deps.sql_reader, deps.llm, policy.sql_policy(), body.query, rows)

        return _run

    return await _run_mode(request, project, "sql", body.query, runner)


@router.post("/projects/{project}/query/global")
async def query_global_endpoint(
    request: Request, project: str, body: QueryRequest
) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_global(deps.repo, body.query, policy.top_k(body.top_k))

        return _run

    return await _run_mode(request, project, "global", body.query, runner)


@router.post("/projects/{project}/query/graph")
async def query_graph_endpoint(
    request: Request, project: str, body: GraphQueryRequest
) -> dict[str, Any]:
    # template vocabulary and the hop ceiling are validated by run_graph
    # itself, IN-ENVELOPE (200 + GUARDRAIL_BLOCKED, rejected-not-clamped) —
    # exactly as the MCP graph tool answers; only the SHAPE (GraphOptions)
    # is this facade's 400 layer, mirroring the tool's typed parameters
    params = _graph_params(body.options)

    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_graph(
                deps.graph,
                deps.repo,
                policy.cypher_policy(),
                params,
                body.query,
                policy.max_graph_hops,
            )

        return _run

    return await _run_mode(request, project, "graph", body.query, runner)


@router.post("/projects/{project}/query/hybrid")
async def query_hybrid_endpoint(
    request: Request, project: str, body: HybridQueryRequest
) -> dict[str, Any]:
    # absent options → the router skips the graph mode with an in-envelope
    # reason (MCP parity: traversal parameters are never fabricated from prose)
    params = None if body.options is None else _graph_params(body.options)

    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, remaining_ms: int) -> Any:
            # hybrid's internal pacer runs on what binding LEFT of the §21
            # budget — never a fresh full max_latency_ms — so the whole
            # request respects the cap (the C8 class-11 face; same rule as
            # the MCP hybrid tool)
            return await run_hybrid(
                deps,
                hybrid_policy(policy, body.top_k, latency_budget_ms=remaining_ms),
                body.query,
                params,
            )

        return _run

    return await _run_mode(request, project, "hybrid", body.query, runner)
