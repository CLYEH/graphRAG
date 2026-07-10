"""Trigger endpoints (BA2e-1) — POST /projects/{p}/ingest|build → 202 job.

Both triggers run the same §5 pipeline (core has ONE pipeline; ingest is its
first stage and every run is convergently idempotent), so they differ only in
the ``kind`` the job records. The flow per request:

* ``create_job_exclusive`` — one active job per project (the contract's 409
  ``JOB_CONFLICT`` "overlapping job"), serialized on the projects row lock so
  concurrent triggers (and a racing project delete) resolve cleanly.
* ``enqueue_build`` IN-BAND, before the request transaction commits — the
  class-12 lesson: a commit followed by a crash-before-enqueue would strand the
  job unmarked (invisible to arq AND the lease reaper). With enqueue-then-commit
  a crash leaves either nothing (rollback) or an orphan arq dispatch that
  no-ops against the absent row — never a committed-but-unenqueued job. The two
  residual losses (Redis drops an acked enqueue; a fast worker dispatches
  before the commit lands and no-ops) ARE committed-but-unenqueued, and the
  reaper's queued-sweep (``find_unenqueued_jobs``) recovers both within the
  enqueue grace.

Request bodies are shape-validated but their fields are LOUDLY rejected until
the pipeline can honor them (see api.schemas — owner decision 2026-07-10).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, Queue, response_meta
from api.envelope import success
from api.idempotency import request_hash, run_idempotent
from api.registry_errors import translate_registry_error
from api.routers._query import reject_null_body
from api.schemas import BuildRequest, IngestRequest, job_accepted_dto
from api.workers.build_worker import enqueue_build
from core.registry import JobConflictError, ProjectNotFoundError, create_job_exclusive

router = APIRouter()

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


async def _trigger(
    request: Request,
    conn: AsyncConnection,
    get_redis: Callable[[], Awaitable[ArqRedis]],
    project: str,
    kind: str,
    endpoint: str,
    idempotency_key: str | None,
) -> JSONResponse:
    # optional body, non-nullable when present (#53 R5 class — shared guard)
    await reject_null_body(request)

    async def produce() -> tuple[int, dict[str, Any]]:
        try:
            job = await create_job_exclusive(conn, project, kind)
        except (ProjectNotFoundError, JobConflictError) as exc:
            raise translate_registry_error(exc) from exc
        # the queue is touched HERE only — a §27 replay, a 409, or a 404 must
        # be served even with Redis unreachable (the Queue dep is a lazy handle)
        await enqueue_build(await get_redis(), project, job.id)
        return 202, success(job_accepted_dto(job), **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=project,
            endpoint=endpoint,
            req_hash=request_hash("POST", request.url.path, await request.body()),
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))


@router.post("/projects/{project}/ingest", tags=["sources"])
async def trigger_ingest_endpoint(
    request: Request,
    conn: Conn,
    get_redis: Queue,
    project: str,
    body: IngestRequest | None = None,  # shape-validates; source_ids rejects loudly
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _trigger(
        request, conn, get_redis, project, "ingest", "triggerIngest", idempotency_key
    )


@router.post("/projects/{project}/build", tags=["builds"])
async def trigger_build_endpoint(
    request: Request,
    conn: Conn,
    get_redis: Queue,
    project: str,
    body: BuildRequest | None = None,  # shape-validates; reason rejects loudly
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _trigger(
        request, conn, get_redis, project, "build", "triggerBuild", idempotency_key
    )
