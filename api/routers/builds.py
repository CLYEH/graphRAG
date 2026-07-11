"""Builds endpoints (BA8) — list/get + activate/rollback (§14, DR-001).

The mutations run on the NEW core seam ``activate_in_caller_txn`` (the
run_bounded_query precedent: one machinery, two facades — core's own
``activate`` wraps the same seam in its own transaction). Composed with the
§27 idempotency machinery, the reservation, the project lifecycle lock, and
the promotion all live in the REQUEST's single transaction: they commit or
roll back together — a crash never strands a key without its effect, a
preflight failure RAISES so the reservation rolls back (a stored failure
would poison the key), and there is exactly ONE pool connection (no #60-R2
convoy).

Rollback is TARGETED (the contract names the build: "the given build is
active again") — core's parameterless ``rollback()`` is the CLI's
one-step-history convenience, not this endpoint. The §20 eval gate is
exempted ONLY for an archived target (history restore of an already-vetted
build, the same exemption core's rollback carries); a READY target through
/rollback is a fresh promotion and keeps the gate — exempting it would make
/rollback a §20 bypass. The exemption is decided on the target's status read
at precheck; the promotion re-validates status and re-runs its checks under
the project lock either way.

The projection stores (drift probes) are acquired at the USE POINT, after
the 404 gates (class 13 — a missing project/build must answer without
touching Neo4j/Qdrant construction or config).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from neo4j.exceptions import DriverError, Neo4jError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, neo4j_driver, qdrant_client, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_id_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query
from api.schemas import build_dto
from core.builds.lifecycle import (
    BuildInfo,
    activate_in_caller_txn,
    get_build_info,
    list_builds_page,
)
from core.registry import ProjectNotFoundError, get_project

router = APIRouter(tags=["builds"])

#: what the mandatory drift probe can raise per store (mirrors health's
#: _PROBE_ERRORS / the MCP layer's _STORE_ERRORS): an outage is the typed
#: 503, never the generic INTERNAL 500 (the BA6a-R4 preflight class — a
#: client dispatching on error.code must know to retry, §22)
_STORE_OUTAGES = (Neo4jError, DriverError, ApiException)

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


async def _require_project(conn: AsyncConnection, project: str) -> None:
    if await get_project(conn, project) is None:
        raise translate_registry_error(ProjectNotFoundError(project))


async def _require_build(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> BuildInfo:
    """Project 404 first, then the build's own 404 — an unknown build must be
    BUILD_NOT_FOUND, never a 409 whose failure text says "not found"."""
    await _require_project(conn, project)
    build = await get_build_info(conn, project, build_id)
    if build is None:
        raise ApiError(
            ErrorCode.BUILD_NOT_FOUND,
            f"build {build_id} not found in project {project!r}",
            details={"build_id": str(build_id)},
        )
    return build


@router.get("/projects/{project}/builds")
async def list_builds_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id")
    await _require_project(conn, project)
    after = decode_id_cursor(cursor)[0] if cursor else None
    builds, next_after = await list_builds_page(conn, project, limit=limit, after_id=after)
    return success(
        [build_dto(b) for b in builds],
        **response_meta(request),
        paginated=True,
        next_cursor=encode_cursor((next_after,)) if next_after else None,
    )


@router.get("/projects/{project}/builds/{build_id}")
async def get_build_endpoint(
    request: Request, conn: Conn, project: str, build_id: uuid.UUID
) -> dict[str, Any]:
    build = await _require_build(conn, project, build_id)
    return success(build_dto(build), **response_meta(request), build_id=build.id)


async def _promote(
    request: Request,
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    *,
    allow_archived: bool,
    history_exempt: bool,
) -> tuple[int, dict[str, Any]]:
    """The shared activate/rollback body — runs INSIDE produce (all prechecks
    behind the replay decision, #53 R2; all effects inside the request txn)."""
    target = await _require_build(conn, project, build_id)
    # the eval gate is exempted only for the archived (history-restore)
    # target — decided on the precheck read; the seam re-validates status
    # and re-runs its checks under the project lock
    apply_gate = not (history_exempt and target.status == "archived")
    qdrant = await qdrant_client(request)
    driver = await neo4j_driver(request)
    try:
        async with driver.session() as session:
            report = await activate_in_caller_txn(
                conn,
                qdrant,
                session,
                project,
                build_id,
                allow_archived=allow_archived,
                apply_eval_gate=apply_gate,
            )
    except RuntimeError as exc:
        # the seam's lost-the-race signal: a concurrent activation changed
        # the target under us — a conflict, not a server bug
        raise ApiError(
            ErrorCode.BUILD_NOT_READY, str(exc), details={"build_id": str(build_id)}
        ) from exc
    except _STORE_OUTAGES as exc:
        # the drift probe is MANDATORY on this path, so a projection-store
        # outage is a typed 503 (fail-closed: the txn rolls back, nothing
        # mutated, the reservation is not poisoned) — never a 500 server-bug
        # envelope (the same mapping inspect/BA7 give this probe)
        raise ApiError(
            ErrorCode.STORE_UNAVAILABLE,
            "drift verification unavailable — a projection store is down (§22)",
            details={"build_id": str(build_id)},
        ) from exc
    if not report.ok:
        # RAISE so the §27 reservation rolls back with the failure — a
        # stored 409 would poison the key (BA1b rule)
        raise ApiError(
            ErrorCode.BUILD_NOT_READY,
            f"activation preflight failed for build {build_id}",
            details={"failures": list(report.failures), "deferred": list(report.deferred)},
        )
    promoted = await get_build_info(conn, project, build_id)
    assert promoted is not None  # promoted in THIS txn under the project lock
    return 200, success(build_dto(promoted), **response_meta(request), build_id=promoted.id)


def _idempotent_route(operation: str) -> Callable[..., Awaitable[JSONResponse]]:
    """activate/rollback share the §27 wiring verbatim (sources.py shape)."""

    async def run(
        request: Request,
        conn: AsyncConnection,
        project: str,
        build_id: uuid.UUID,
        idempotency_key: str | None,
        *,
        allow_archived: bool,
        history_exempt: bool,
    ) -> JSONResponse:
        async def produce() -> tuple[int, dict[str, Any]]:
            return await _promote(
                request,
                conn,
                project,
                build_id,
                allow_archived=allow_archived,
                history_exempt=history_exempt,
            )

        if idempotency_key:
            status, resp = await run_idempotent(
                conn,
                key=idempotency_key,
                project=project,
                endpoint=operation,
                req_hash=request_hash("POST", request.url.path, await request.body()),
                produce=produce,
            )
            return JSONResponse(status_code=status, content=resp)
        status, resp = await produce()
        return JSONResponse(status_code=status, content=jsonable_encoder(resp))

    return run


_run_activate = _idempotent_route("activateBuild")
_run_rollback = _idempotent_route("rollbackBuild")


@router.post("/projects/{project}/builds/{build_id}/activate")
async def activate_build_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    build_id: uuid.UUID,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _run_activate(
        request,
        conn,
        project,
        build_id,
        idempotency_key,
        allow_archived=False,
        history_exempt=False,
    )


@router.post("/projects/{project}/builds/{build_id}/rollback")
async def rollback_build_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    build_id: uuid.UUID,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _run_rollback(
        request, conn, project, build_id, idempotency_key, allow_archived=True, history_exempt=True
    )
