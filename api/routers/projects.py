"""Projects endpoints (BA1b) — the §15 CRUD over the BA1a registry.

Every handler stamps the §15 envelope from the middleware's request state and
delegates to ``core.registry``; domain errors go through the single translation
point (``api.registry_errors``). The two writes accept an Idempotency-Key; the
reads/PATCH/DELETE are naturally idempotent and take none.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, ConnProvider, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_project_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_unsupported_query
from api.schemas import ProjectCreate, ProjectUpdate, project_dto
from core.config import get_settings
from core.paths import safe_project_subdir
from core.registry import (
    ProjectExistsError,
    ProjectHasActiveJobsError,
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
async def delete_project_endpoint(open_conn: ConnProvider, project: str) -> Response:
    # QA7/D16: the registry row was the ONLY thing delete removed, so a
    # project's managed-corpus uploads outlived it on disk with no endpoint
    # able to list or remove them — unbounded growth and an unintended
    # retention surface.
    #
    # The transaction is owned HERE rather than taken as a request-scoped
    # ``Conn``, because WHEN it commits decides which failure is possible.
    # A yield-dep commits only after the response is sent, so with ``Conn``
    # the real order is delete → rmtree → 204 → COMMIT, and a commit failure
    # after a successful rmtree would leave the client told 204, the project
    # row alive, and its corpus irreversibly gone. Owning the block makes the
    # row durable BEFORE any file is touched; the sibling upload writer
    # refuses the mirror-image residue for the same reason.
    #
    # Files go after the commit, deliberately the OPPOSITE of prune's
    # "projections first, Postgres last": that rule assumes the truth-delete
    # always proceeds, whereas this one can legitimately REFUSE (builds
    # present, active jobs) — and those refusals raise inside the block, so
    # they touch no files. The residual is a crash between commit and rmtree,
    # which leaks the directory exactly as today: never worse, and loud.
    async with open_conn() as conn:
        try:
            existed = await delete_project(conn, project)
        except (ProjectHasBuildsError, ProjectHasActiveJobsError) as exc:
            raise translate_registry_error(exc) from exc
        if not existed:
            raise _not_found(project)
    _delete_upload_dir(project)
    return Response(status_code=204)


def _delete_upload_dir(project: str) -> None:
    """Remove ``<upload_corpus_dir>/<project>/`` after the project row is gone.

    The path is resolved through :func:`core.paths.safe_project_subdir`, the
    same guard the upload writer uses — a name that is not a safe single path
    component (``..``, absolute, separators) resolves to None and nothing is
    removed, so a crafted project name cannot direct a delete outside the
    corpus root. A project that never received an upload has no directory and
    that is a no-op, not an error.
    """
    corpus_dir = safe_project_subdir(Path(get_settings().upload_corpus_dir), project)
    if corpus_dir is None or not corpus_dir.exists():
        return
    shutil.rmtree(corpus_dir)
