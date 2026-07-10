"""Query playground endpoints (BA6a) — semantic/sql/global over the ACTIVE
build; graph/hybrid land in BA6b on the same seam.

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
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, project_query_context, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.registry_errors import translate_registry_error
from api.schemas import QueryRequest
from core.llm.factory import LLMNotConfiguredError
from core.mcp.policy import PolicyError, QueryPolicy, query_policy_from_mapping
from core.mcp.server import run_bounded_query
from core.query.global_reports import global_summary as run_global
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.registry import ProjectNotFoundError, get_project
from core.stores.repo import NoActiveBuildError

router = APIRouter(tags=["query"])

_NIL_BUILD = "00000000-0000-0000-0000-000000000000"


async def _load_policy(conn: AsyncConnection, project: str) -> QueryPolicy:
    """Project 404 first, then the registry-config policy (the BA3c seam)."""
    proj = await get_project(conn, project)
    if proj is None:
        raise translate_registry_error(ProjectNotFoundError(project))
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


async def _run_mode(
    request: Request,
    conn: AsyncConnection,
    project: str,
    mode: str,
    body: QueryRequest,
    runner: Any,
) -> dict[str, Any]:
    policy = await _load_policy(conn, project)
    try:
        context = project_query_context(request, project)
    except LLMNotConfiguredError as exc:
        # GAP (module docstring): the mode's model dependency is unavailable —
        # a typed 503 naming the missing config, never a coarse 500
        raise ApiError(ErrorCode.STORE_UNAVAILABLE, str(exc)) from exc
    try:
        mcp_dict = await run_bounded_query(
            context, policy, f"query_{mode}", body.query, runner(policy)
        )
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
    request: Request, conn: Conn, project: str, body: QueryRequest
) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_semantic(
                deps.repo, deps.vectors, deps.embedder, body.query, policy.top_k(body.top_k)
            )

        return _run

    return await _run_mode(request, conn, project, "semantic", body, runner)


@router.post("/projects/{project}/query/sql")
async def query_sql_endpoint(
    request: Request, conn: Conn, project: str, body: QueryRequest
) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_sql(
                deps.sql_reader, deps.llm, policy.sql_policy(), body.query, policy.sql_rows()
            )

        return _run

    return await _run_mode(request, conn, project, "sql", body, runner)


@router.post("/projects/{project}/query/global")
async def query_global_endpoint(
    request: Request, conn: Conn, project: str, body: QueryRequest
) -> dict[str, Any]:
    def runner(policy: QueryPolicy) -> Any:
        async def _run(deps: Any, _remaining_ms: int) -> Any:
            return await run_global(deps.repo, body.query, policy.top_k(body.top_k))

        return _run

    return await _run_mode(request, conn, project, "global", body, runner)
