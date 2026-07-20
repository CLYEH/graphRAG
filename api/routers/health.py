"""Observability endpoints (BA7) — §19 health, the metrics snapshot, §20 eval.

One producer, two projections: ``/health`` and ``/metrics`` BOTH serve
``core.observability.health.health_report`` (class 5 — the snapshot can never
disagree with the light computed from the same numbers); ``/metrics``
reprojects the report's metrics dict alone. ``/eval`` serves
``latest_eval_payload`` (§20's gate predicates, measured-facts nulls).

PRECEDENCE NOTE — deliberately NOT the query surface's 404→409 chain: this
surface's concept is OBSERVATION. Health/metrics/eval report on broken and
bootstrap states (health_report handles ``active=None``; eval serves the
all-null report), so a project with no active build is a legitimate 200,
never NO_ACTIVE_BUILD — the "precedence belongs to the concept" lesson cuts
both ways. The project 404 is the only gate.

``meta.build_id`` names the build the payload is ABOUT (health/metrics: the
active build whose content the counts are scoped to; eval: the build whose
report is served) and stays null when there is none.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, neo4j_driver, qdrant_client, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.registry_errors import translate_registry_error
from core.config import Settings, get_settings
from core.mcp.addressing import resolved_advertised_host
from core.observability.health import HealthReport, health_report, latest_eval_payload
from core.registry import ProjectNotFoundError, get_project

router = APIRouter(tags=["health"])


async def _require_project(conn: AsyncConnection, project: str) -> None:
    if await get_project(conn, project) is None:
        raise translate_registry_error(ProjectNotFoundError(project))


async def _report(request: Request, conn: AsyncConnection, project: str) -> HealthReport:
    """Project 404 first, then §19's report. The projection stores go in as
    PROVIDERS the report invokes only when the drift probe actually runs —
    a missing/bootstrap project answers without touching Neo4j/Qdrant
    construction or config (the #53 R3 eager-acquisition class; Codex #62:
    resolving them as route dependencies made even the 404 depend on store
    config being valid)."""
    await _require_project(conn, project)
    return await health_report(
        conn,
        project,
        vector_provider=lambda: qdrant_client(request),
        graph_provider=lambda: neo4j_driver(request),
    )


@router.get("/projects/{project}/health")
async def get_health_endpoint(request: Request, project: str, conn: Conn) -> dict[str, Any]:
    report = await _report(request, conn, project)
    return success(report.to_payload(), **response_meta(request), build_id=report.active_build_id)


@router.get("/projects/{project}/metrics")
async def get_metrics_endpoint(request: Request, project: str, conn: Conn) -> dict[str, Any]:
    report = await _report(request, conn, project)
    return success(report.metrics, **response_meta(request), build_id=report.active_build_id)


@router.get("/projects/{project}/eval")
async def get_eval_endpoint(request: Request, project: str, conn: Conn) -> dict[str, Any]:
    await _require_project(conn, project)
    payload = await latest_eval_payload(conn, project)
    served = payload["build_id"]
    return success(
        payload,
        **response_meta(request),
        build_id=uuid.UUID(served) if isinstance(served, str) else None,
    )


def _advertised_host(settings: Settings, request: Request) -> str:
    """The host an EXTERNAL agent should dial, via the SHARED resolver in
    ``core.mcp.addressing`` (the serve-mcp CLI warning uses the same one, so
    the two can never disagree about what the Console advertises).

    The wildcard fallback comes from the client-supplied ``Host`` header the
    Console reached this API on — same machine in the DR-012 single-host
    deploy; a reverse proxy that rewrites ``Host`` is exactly when
    ``mcp_public_host`` becomes mandatory. A caller can only influence its
    OWN uncached response, so the accident model applies, not the adversarial
    one.

    An unusable configured value (host with a port, whitespace, scoped IPv6 —
    no valid URI can carry these) fails LOUD as a typed INTERNAL error naming
    the setting, instead of a 200 whose ``url`` violates ``format: uri``.
    """
    try:
        host = resolved_advertised_host(settings, reached_host=request.url.hostname or "localhost")
    except ValueError as exc:
        raise ApiError(
            ErrorCode.INTERNAL,
            f"mcp_public_host/mcp_http_host cannot form a valid URL authority: {exc}",
        ) from exc
    assert host is not None  # reached_host was supplied, so None is impossible
    return host


@router.get("/projects/{project}/mcp")
async def get_mcp_info_endpoint(request: Request, project: str, conn: Conn) -> dict[str, Any]:
    """The project's DR-012 gateway connection info (contract v1.3).

    The URL is DERIVED from the settings the gateway binds to
    (``mcp_http_host``/``mcp_http_port``) and the path shape it routes
    (``/mcp/<project>``), so a settings change moves both together. Two
    corrections the naive f-string gets wrong (Codex #113 P1):

    * a BIND is an interface, not an address — ``0.0.0.0``/``::`` mean "every
      interface" and are meaningless to a client, which would resolve them
      locally and never reach this gateway. ``_advertised_host`` substitutes
      an address an external agent can actually dial: the explicit
      ``mcp_public_host`` setting if the operator set one, else the host the
      Console itself was reached on (same machine in the single-host deploy).
    * an IPv6 literal needs authority BRACKETS (``[::1]:8300``), or the port
      colon is ambiguous and the URL is malformed.

    NOTE the one fork the contract leaves open: ``graphrag serve-mcp
    --host/--port`` overrides the bind for that process WITHOUT changing the
    settings, and this payload follows the settings (the frozen contract says
    "derived from the server's GRAPHRAG_MCP_HTTP_HOST/PORT settings"). The CLI
    warns loudly when an override diverges and names both addresses.

    The project segment is percent-encoded with an empty ``safe`` set: the
    gateway matches the RAW path and keeps an encoded slash inside its segment
    (Codex #93 R3), so emitting the raw name would advertise a URL that
    resolves to a DIFFERENT project.

    ``meta.build_id`` is null: this payload is about the connection surface,
    not about any build's content (the observation-precedence note above).
    """
    await _require_project(conn, project)
    settings = get_settings()
    host = _advertised_host(settings, request)
    url = f"http://{host}:{settings.mcp_http_port}/mcp/{quote(project, safe='')}"
    return success(
        {"transport": "streamable-http", "auth": "none", "url": url},
        **response_meta(request),
        build_id=None,
    )
