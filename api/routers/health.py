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

from fastapi import APIRouter, Request
from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, Graph, Vectors, response_meta
from api.envelope import success
from api.registry_errors import translate_registry_error
from core.observability.health import HealthReport, health_report, latest_eval_payload
from core.registry import ProjectNotFoundError, get_project

router = APIRouter(tags=["health"])


async def _require_project(conn: AsyncConnection, project: str) -> None:
    if await get_project(conn, project) is None:
        raise translate_registry_error(ProjectNotFoundError(project))


async def _report(
    conn: AsyncConnection, qdrant: AsyncQdrantClient, driver: AsyncDriver, project: str
) -> HealthReport:
    """Project 404 first, then §19's report — the drift probe opens its Neo4j
    session at the use point and closes it with the request's report."""
    await _require_project(conn, project)
    async with driver.session() as session:
        return await health_report(conn, qdrant, session, project)


@router.get("/projects/{project}/health")
async def get_health_endpoint(
    request: Request, project: str, conn: Conn, qdrant: Vectors, driver: Graph
) -> dict[str, Any]:
    report = await _report(conn, qdrant, driver, project)
    return success(report.to_payload(), **response_meta(request), build_id=report.active_build_id)


@router.get("/projects/{project}/metrics")
async def get_metrics_endpoint(
    request: Request, project: str, conn: Conn, qdrant: Vectors, driver: Graph
) -> dict[str, Any]:
    report = await _report(conn, qdrant, driver, project)
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
