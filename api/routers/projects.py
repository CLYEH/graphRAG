"""Projects endpoints (BA1b) — the §15 CRUD over the BA1a registry.

Every handler stamps the §15 envelope from the middleware's request state and
delegates to ``core.registry``; domain errors go through the single translation
point (``api.registry_errors``). The two writes accept an Idempotency-Key; the
reads/PATCH/DELETE are naturally idempotent and take none.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_project_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query
from api.schemas import ProjectCreate, ProjectUpdate, project_dto
from core.registry import (
    ProjectExistsError,
    ProjectHasBuildsError,
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
)

router = APIRouter(tags=["projects"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


def _not_found(project: str) -> ApiError:
    return ApiError(
        ErrorCode.PROJECT_NOT_FOUND, f"project {project!r} not found", details={"project": project}
    )


@router.get("/projects")
async def list_projects_endpoint(
    request: Request,
    conn: Conn,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "created_at")
    after = decode_project_cursor(cursor) if cursor else None
    projects, next_after = await list_projects(conn, limit=limit, after=after)
    return success(
        [project_dto(p) for p in projects],
        **response_meta(request),
        paginated=True,
        next_cursor=encode_cursor(next_after) if next_after else None,
    )


@router.post("/projects")
async def create_project_endpoint(
    request: Request,
    conn: Conn,
    body: ProjectCreate,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    async def produce() -> tuple[int, dict[str, Any]]:
        try:
            p = await create_project(
                conn,
                name=body.name,
                display_name=body.display_name,
                description=body.description,
                config=body.config,
            )
        except ProjectExistsError as exc:
            raise translate_registry_error(exc) from exc
        return 201, success(project_dto(p), **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=body.name,
            endpoint="createProject",
            req_hash=request_hash("POST", request.url.path, await request.body()),
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))


@router.get("/projects/{project}")
async def get_project_endpoint(request: Request, conn: Conn, project: str) -> dict[str, Any]:
    p = await get_project(conn, project)
    if p is None:
        raise _not_found(project)
    return success(project_dto(p), **response_meta(request))


@router.patch("/projects/{project}")
async def update_project_endpoint(
    request: Request, conn: Conn, project: str, body: ProjectUpdate
) -> dict[str, Any]:
    # exclude_unset bridges Pydantic to the registry's _UNSET sentinel: an
    # omitted field never enters the patch (stays unchanged); an explicit null
    # is passed through (clears the column)
    patch = body.model_dump(exclude_unset=True)
    p = await update_project(conn, project, **patch)
    if p is None:
        raise _not_found(project)
    return success(project_dto(p), **response_meta(request))


@router.delete("/projects/{project}", status_code=204)
async def delete_project_endpoint(conn: Conn, project: str) -> Response:
    try:
        existed = await delete_project(conn, project)
    except ProjectHasBuildsError as exc:
        raise translate_registry_error(exc) from exc
    if not existed:
        raise _not_found(project)
    return Response(status_code=204)
