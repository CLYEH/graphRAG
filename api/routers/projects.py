"""Projects endpoints (BA1b) — the §15 CRUD over the BA1a registry.

Every handler stamps the §15 envelope from the middleware's request state and
delegates to ``core.registry``; domain errors go through the single translation
point (``api.registry_errors``). The two writes accept an Idempotency-Key; the
reads/PATCH/DELETE are naturally idempotent and take none.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, ConnProvider, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_sorted_cursor, encode_sorted_cursor, scope_fingerprint
from api.registry_errors import translate_registry_error
from api.routers._corpus import reject_unsafe_corpus_path
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

_log = logging.getLogger(__name__)

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
    # QA8/D6: this minted an untagged (created_at, name) pair, and a SOURCES
    # cursor — same arity, same (datetime, str) types — decoded here cleanly,
    # so one project's source listing silently re-anchored the global project
    # list. The registry has exactly one global listing, so the scope is a
    # constant; the tag is what makes the token non-interchangeable, not the
    # scope's information content.
    tag = f"created_at:desc|{scope_fingerprint('projects', '', None, {})}"
    after = decode_sorted_cursor(cursor, tag, (datetime, str)) if cursor else None
    projects, next_after = await list_projects(conn, limit=limit, after=after)
    return success(
        [project_dto(p) for p in projects],
        **response_meta(request),
        paginated=True,
        next_cursor=encode_sorted_cursor(tag, next_after) if next_after else None,
    )


@router.post("/projects")
async def create_project_endpoint(
    request: Request,
    conn: Conn,
    body: ProjectCreate,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    # QA10/Codex #149: a project key has to survive THREE surfaces, and the
    # first two are pure string rules the schema can express. The third is the
    # canonical corpus ``file://`` uri, whose rule lives in the source resolver
    # — ``foo|bar`` is a safe path component that ``as_uri()`` encodes to a form
    # no build can resolve, so the project was creatable and then every upload
    # 400'd forever. Asked here with the UPLOAD ENDPOINT'S OWN HELPER rather
    # than restated: a second copy of the rule is how the two drift, which is
    # exactly how this surface came to be missed. It lives in the shared
    # `_corpus` module rather than in either router (the `_query` pattern).
    #
    # In the handler rather than the Pydantic validator because it needs
    # settings and does a filesystem ``resolve()`` — the same reason
    # ``reject_unsafe_corpus_path`` is sync and runs before any file I/O.
    reject_unsafe_corpus_path(get_settings(), body.name)

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
    # ``Conn``, because WHEN it commits decides which failures are possible: a
    # yield-dep commits only after the response is sent, so the row would not
    # be durable while this handler still had work to do.
    #
    # The corpus is addressed by NAME, and a name is reusable the moment its
    # row is gone — multiple one-worker instances sharing a corpus root is the
    # documented scaling shape. So the directory is DETACHED to a unique
    # tombstone INSIDE the deleting transaction, while the row lock still
    # blocks any concurrent create of that name; only the tombstone is removed
    # afterwards. Nothing a later project writes can be reached by this
    # request's cleanup, so there is no window to lose. (A post-commit
    # re-check only MOVED the window — the SELECT's transaction ends before
    # the rmtree, and a future INSERT has no row to lock. Codex #145 P1.)
    #
    # The two failure paths follow from detaching inside the transaction: a
    # REFUSAL (builds present, active jobs) raises before the rename and so
    # touches nothing, and a failure at COMMIT re-attaches the directory —
    # otherwise a project that still exists would have lost its corpus.
    # The rename is SYNCHRONOUS, and that is load-bearing: no await separates
    # it from the assignment below, so between those two points no other code
    # runs and CancelledError cannot be delivered — the handler therefore knows
    # about the tombstone if and only if the rename happened. Threading it
    # breaks that invariant rather than improving it: `asyncio.to_thread`
    # cannot cancel the thread it started AND the awaiter resumes with
    # CancelledError without waiting for it, so a cancellation (worker
    # shutdown, an infra timeout) can leave the rename landing after the
    # rollback has already decided there was nothing to undo — the project and
    # its managed sources alive while their corpus sits under an
    # undiscoverable `.deleting-*` name. Shielding does not help; what is lost
    # is the assignment, not the work. Only rmtree, the unbounded part, is
    # worth a thread.
    tombstone: Path | None = None
    try:
        async with open_conn() as conn:
            try:
                existed = await delete_project(conn, project)
            except (ProjectHasBuildsError, ProjectHasActiveJobsError) as exc:
                raise translate_registry_error(exc) from exc
            if not existed:
                raise _not_found(project)
            tombstone = _detach_upload_dir(project)
    except BaseException:
        if tombstone is not None:
            try:
                # SYNCHRONOUS on purpose: this runs on the cancellation path,
                # where awaiting again may be interrupted immediately. A rename
                # is a fast metadata operation, and giving the corpus back must
                # not depend on being allowed to await.
                _reattach_upload_dir(tombstone, project)
            except OSError:
                # best-effort work must not MASK the failure it is recovering
                # from: letting a rename error replace the original exception
                # would show the operator the wrong cause entirely
                _log.warning("could not re-attach %s after a failed delete", tombstone)
        raise
    if tombstone is not None:
        # Off the event loop: an accumulated corpus is unbounded in size and
        # file count, and a slow shared filesystem would otherwise stall every
        # unrelated request on this worker.
        #
        # Errors are SWALLOWED here on purpose. The delete genuinely succeeded
        # — the row is committed — so raising would report a failure that did
        # not happen, and a retry would 404. What is left behind is an inert,
        # self-identifying tombstone no code path scans, so the honest handling
        # is to answer 204 and make the leftover DISCOVERABLE rather than
        # invisible: hence the warning below, not silence.
        await asyncio.to_thread(shutil.rmtree, tombstone, ignore_errors=True)
        if await asyncio.to_thread(tombstone.exists):
            _log.warning("upload corpus tombstone survives cleanup: %s", tombstone)
    return Response(status_code=204)


def _detach_upload_dir(project: str) -> Path | None:
    """Rename ``<upload_corpus_dir>/<project>/`` aside, returning the tombstone.

    Called INSIDE the deleting transaction, while the row lock still blocks a
    concurrent create of this name. The rename is what makes cleanup safe: it
    is atomic, so after it the name is free for a new project to occupy with a
    FRESH directory, and this request's remaining work names only the
    tombstone. A cleanup that still addressed the project name after the
    commit could reach a later project's uploads — deleting live documents,
    which is worse than the leak this change removes.

    The path is resolved through :func:`core.paths.safe_project_subdir`, the
    same guard the upload writer uses, so a name that is not a safe single
    path component (``..``, absolute, separators) resolves to None and nothing
    is touched. A project that never received an upload has no directory, and
    that is a no-op returning None rather than an error.
    """
    corpus_dir = safe_project_subdir(Path(get_settings().upload_corpus_dir), project)
    if corpus_dir is None or not corpus_dir.exists():
        return None
    tombstone = corpus_dir.with_name(f".deleting-{corpus_dir.name}-{uuid.uuid4().hex}")
    corpus_dir.rename(tombstone)
    return tombstone


def _reattach_upload_dir(tombstone: Path, project: str) -> None:
    """Undo :func:`_detach_upload_dir` when the deleting transaction did not
    commit — otherwise a project that still exists would have lost its corpus.

    Best-effort by design: if the original name has since been taken, the
    tombstone is left in place rather than overwriting whatever now occupies
    it. That leaves a recoverable directory instead of destroying one.
    """
    corpus_dir = safe_project_subdir(Path(get_settings().upload_corpus_dir), project)
    if corpus_dir is None or corpus_dir.exists():
        return
    tombstone.rename(corpus_dir)
