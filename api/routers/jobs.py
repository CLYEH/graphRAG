"""Job endpoints (BA2e) — status, cooperative cancel, and the SSE stream,
all over the jobs SoR.

GET and the event stream serve the durable jobs row (§27.7 — never arq's own
job state, which is deliberately unreadable: the worker keeps no results).
Cancel flips the cooperative flag — the worker stops at the next step
boundary — and returns 202 with the job's CURRENT status; cancelling an
already-terminal job replays its terminal state as a no-op rather than
erroring (the flag is only ever set while the job is active).

The SSE stream (BA2e-2) polls the row on short-lived per-poll connections —
NEVER the request's ``db_conn``: a streaming generator outlives its handler,
so the request transaction would sit idle-in-transaction for the stream's
whole life (and pin one pool connection per subscriber). Frozen §27.2 frames:
``job.update`` on the initial state and on every observed change, then
``job.done``/``job.failed`` once and the stream ends.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import replay_stored, request_hash, run_idempotent
from api.registry_errors import translate_registry_error
from api.schemas import job_accepted_dto, job_dto, job_event_dto
from core.config import get_settings
from core.registry import Job, JobNotFoundError, get_job, get_job_at, request_cancel

router = APIRouter(tags=["jobs"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]

#: One SoR observation: (job, DB clock at the read) — None once the row is gone.
PollJob = Callable[[uuid.UUID], Awaitable[tuple[Job, datetime] | None]]


def job_poller(request: Request) -> PollJob:
    """The SSE stream's SoR read seam: each poll borrows a connection from the
    app engine for ONE single-row SELECT and returns it — no transaction is
    held between frames (see the module docstring for why ``db_conn`` must not
    back the stream). A dependency so tests can substitute scripted
    observations without a database."""

    async def _poll(job_id: uuid.UUID) -> tuple[Job, datetime] | None:
        engine = request.app.state.engine
        async with engine.connect() as conn:
            return await get_job_at(conn, job_id)

    return _poll


#: Terminal status → frozen §27.2 event name. ``cancelled`` maps to
#: ``job.failed``: the frozen event vocabulary has no job.cancelled, and a
#: cancelled job is a failure-flavored terminal in this codebase (a cancelled
#: BUILD lands status='failed' with metrics.cancelled, §14/BA2c) — the frame's
#: ``status`` field still carries the exact 'cancelled', so nothing is lost.
_TERMINAL_EVENTS = {"done": "job.done", "failed": "job.failed", "cancelled": "job.failed"}


def _sse_frame(event: str, payload: dict[str, Any]) -> str:
    """One ``text/event-stream`` frame: named event + single-line JSON data."""
    data = json.dumps(jsonable_encoder(payload), separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


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


async def _event_stream(
    poll: PollJob,
    job_id: uuid.UUID,
    interval: float,
    first: tuple[Job, datetime],
) -> AsyncIterator[str]:
    """The §27.2 frame sequence for one subscriber, starting from the
    observation the endpoint prechecked. Emits the CURRENT state immediately
    (a late subscriber to a terminal job gets exactly its terminal frame),
    then a ``job.update`` per observed change of the frozen frame fields, then
    the terminal event once — and ends. A row that vanishes mid-stream (its
    project was deleted — legal once the job is terminal) ends the stream
    WITHOUT a fabricated terminal frame: the SoR never held such a state, and
    a reconnect gets an honest 404. Client disconnects cancel the generator
    inside the sleep; no resource is held between polls. A transient DB error
    on a mid-stream poll propagates and severs the stream — deliberate:
    standard SSE clients auto-reconnect (and get the envelope's honest error
    if the outage persists), whereas swallowing it here would freeze a silent,
    eternally-pending stream."""
    last: dict[str, Any] | None = None
    observed: tuple[Job, datetime] | None = first
    while True:
        if observed is None:
            return
        job, ts = observed
        payload = job_event_dto(job, ts)
        terminal = _TERMINAL_EVENTS.get(job.status)
        if terminal is not None:
            yield _sse_frame(terminal, payload)
            return
        # change detection excludes ts — a fresh clock alone is not progress
        state = {k: v for k, v in payload.items() if k != "ts"}
        if state != last:
            yield _sse_frame("job.update", payload)
            last = state
        await asyncio.sleep(interval)
        observed = await poll(job_id)


@router.get("/jobs/{job_id}/events")
async def stream_job_events_endpoint(
    job_id: uuid.UUID,
    poll: Annotated[PollJob, Depends(job_poller)],
) -> StreamingResponse:
    # deliberately NOT Conn: a yield-dependency stays open until the RESPONSE
    # completes, so the request transaction would idle for the stream's whole
    # life — the precheck observes through the same short-lived poll seam the
    # stream uses, and its result seeds the first frame
    first = await poll(job_id)
    if first is None:
        raise _job_not_found(job_id)
    return StreamingResponse(
        _event_stream(poll, job_id, get_settings().sse_poll_interval_seconds, first),
        media_type="text/event-stream",
        # SSE responses must never be cached or transformed by intermediaries
        headers={"Cache-Control": "no-cache"},
    )
