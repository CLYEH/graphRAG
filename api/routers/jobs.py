"""Job endpoints (BA2e-1) — status + cooperative cancel over the jobs SoR.

GET serves the durable jobs row (§27.7 — never arq's own job state, which is
deliberately unreadable: the worker keeps no results). Cancel flips the
cooperative flag — the worker stops at the next step boundary — and returns 202
with the job's CURRENT status; cancelling an already-terminal job replays its
terminal state as a no-op rather than erroring (the flag is only ever set while
the job is active). The SSE stream (`/jobs/{id}/events`) is BA2e-2.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import replay_stored, request_hash, run_idempotent
from api.registry_errors import translate_registry_error
from api.schemas import job_accepted_dto, job_dto
from core.registry import JobNotFoundError, get_job, request_cancel

router = APIRouter(tags=["jobs"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


def _job_not_found(job_id: uuid.UUID) -> ApiError:
    return ApiError(
        ErrorCode.JOB_NOT_FOUND, f"job {job_id} does not exist", details={"job_id": job_id}
    )


@router.get("/jobs/{job_id}")
async def get_job_endpoint(request: Request, conn: Conn, job_id: uuid.UUID) -> dict[str, Any]:
    job = await get_job(conn, job_id)
    if job is None:
        raise _job_not_found(job_id)
    return success(job_dto(job), **response_meta(request))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(
    request: Request,
    conn: Conn,
    job_id: uuid.UUID,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    async def produce() -> tuple[int, dict[str, Any]]:
        try:
            job = await request_cancel(conn, job_id)
        except JobNotFoundError as exc:
            raise translate_registry_error(exc) from exc
        return 202, success(job_accepted_dto(job), **response_meta(request))

    if idempotency_key:
        req_hash = request_hash("POST", request.url.path, await request.body())
        # §27 replay/conflict must be decided BEFORE the job precheck: the job
        # row can legitimately be gone by the retry (a terminal job CASCADE-
        # deletes with its project), and a live key must still replay its
        # stored response — or 409 on a different hash — never JOB_NOT_FOUND.
        replayed = await replay_stored(conn, key=idempotency_key, req_hash=req_hash)
        if replayed is not None:
            status, resp = replayed
            return JSONResponse(status_code=status, content=resp)
        # fresh request: the idempotency row is keyed under a project, so
        # resolve the job now (an unknown job is a 404 that never reserves)
        job = await get_job(conn, job_id)
        if job is None:
            raise _job_not_found(job_id)
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=job.project,
            endpoint="cancelJob",
            req_hash=req_hash,
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))
