"""Sources endpoints (BA1b) — list/add under a project.

listSources pre-checks the project (a listing under a missing project is a 404,
not an empty 200). addSource accepts an Idempotency-Key; a missing project maps
to PROJECT_NOT_FOUND via the single translation point.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_source_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query
from api.schemas import SourceCreate, source_dto
from core.registry import ProjectNotFoundError, add_source, get_project, list_sources

router = APIRouter(tags=["sources"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


async def _require_project(conn: Conn, project: str) -> None:
    if await get_project(conn, project) is None:
        raise ApiError(
            ErrorCode.PROJECT_NOT_FOUND,
            f"project {project!r} not found",
            details={"project": project},
        )


@router.get("/projects/{project}/sources")
async def list_sources_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "added_at")
    await _require_project(conn, project)
    after = decode_source_cursor(cursor) if cursor else None
    sources, next_after = await list_sources(conn, project, limit=limit, after=after)
    return success(
        [source_dto(s) for s in sources],
        **response_meta(request),
        paginated=True,
        next_cursor=encode_cursor(next_after) if next_after else None,
    )


@router.post("/projects/{project}/sources")
async def add_source_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    body: SourceCreate,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    async def produce() -> tuple[int, dict[str, Any]]:
        try:
            s = await add_source(
                conn, project, uri=body.uri, kind=body.kind, metadata=body.metadata
            )
        except ProjectNotFoundError as exc:
            raise translate_registry_error(exc) from exc
        return 201, success(source_dto(s), **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=project,
            endpoint="addSource",
            req_hash=request_hash("POST", request.url.path, await request.body()),
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))
