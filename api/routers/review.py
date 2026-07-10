"""Review endpoints (BA5) — the Console's §17 merge-candidate queue.

List serves the ACTIVE build's candidates through the DR-006 repo (the BA3
binding/cursor pattern verbatim); the three decision verbs call
``core.resolve.decisions.decide_merge_candidate``, whose single transaction
locks the candidate, validates the §17 transition, writes the DR-003
carry-forward ledger entry (keyed by the same ``merge_key`` resolve reads —
including for DEFER, which must block a future auto-merge), and stamps the
audit trail. Idempotency-Key rides the BA1b machinery with every precheck
INSIDE produce (the #53 R2 ordering rule: a stored response replays even
after the candidate's project is gone).

``decided_by`` is the §23 placeholder principal ``"console"`` — a fixed
curator marker (never ``"auto"``, which §27.3 precedence reserves for the
pipeline); real names arrive when auth lands, changing only this constant's
source.

GAP (registry_errors precedent): the frozen enum has no candidate-not-found
code and no review-conflict code — a missing candidate is the framework's
true 404 with the coarse code; an illegal §17 transition is a 400
VALIDATION_ERROR with machine-readable {status, decision} details.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_id_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_null_body, reject_unsupported_query
from api.schemas import ReviewDecisionRequest, merge_candidate_dto
from core.registry import ProjectNotFoundError, get_project
from core.resolve.decisions import (
    InvalidReviewTransitionError,
    MergeCandidateNotFoundError,
    decide_merge_candidate,
)
from core.stores import tables
from core.stores.repo import ActiveBinding, BuildScopedRepo, NoActiveBuildError
from core.stores.repo import resolve_active_binding as _resolve_active_binding

router = APIRouter(tags=["review"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]

#: §23 placeholder principal — the Console surface as curator (never "auto")
_CONSOLE_DECIDER = "console"


async def _bind(conn: Any, project: str) -> ActiveBinding:
    """Project 404 first, then the DR-001 active resolution (409 if none)."""
    try:
        if await get_project(conn, project) is None:
            raise ProjectNotFoundError(project)
        return await _resolve_active_binding(conn, project)
    except (ProjectNotFoundError, NoActiveBuildError) as exc:
        raise translate_registry_error(exc) from exc


@router.get("/projects/{project}/merge-candidates")
async def list_merge_candidates_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id")
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    mc = tables.merge_candidates
    where = []
    if cursor:
        (after_id,) = decode_id_cursor(cursor)
        where.append(mc.c.id < after_id)
    rows = await repo.fetch_page(mc, *where, order_by=[mc.c.id.desc()], limit=limit + 1)
    page = rows[:limit]
    next_cursor = encode_cursor((page[-1].id,)) if len(rows) > limit else None
    return success(
        [merge_candidate_dto(r) for r in page],
        **response_meta(request),
        build_id=binding.build_id,
        paginated=True,
        next_cursor=next_cursor,
    )


async def _decide(
    request: Request,
    conn: Any,
    project: str,
    candidate_id: uuid.UUID,
    verb: str,
    endpoint: str,
    body: ReviewDecisionRequest | None,
    idempotency_key: str | None,
) -> JSONResponse:
    # optional body, non-nullable when present (#53 R5 class — shared guard)
    await reject_null_body(request)
    reason = body.reason if body is not None else None

    async def produce() -> tuple[int, dict[str, Any]]:
        # every precheck INSIDE produce (#53 R2 ordering rule): a replayed
        # decision must win even if the candidate's world has since vanished
        binding = await _bind(conn, project)
        try:
            candidate = await decide_merge_candidate(
                conn,
                project=project,
                build_id=binding.build_id,
                candidate_id=candidate_id,
                verb=verb,
                decided_by=_CONSOLE_DECIDER,
                reason=reason,
            )
        except MergeCandidateNotFoundError as exc:
            # GAP: true 404 status, coarse frozen code (module docstring)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidReviewTransitionError as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,  # GAP: no frozen review-conflict code
                str(exc),
                details={"status": exc.current, "decision": exc.verb},
            ) from exc
        return 200, success(
            merge_candidate_dto(candidate),
            **response_meta(request),
            build_id=binding.build_id,
        )

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


@router.post("/projects/{project}/merge-candidates/{candidate_id}/approve")
async def approve_merge_candidate_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    candidate_id: uuid.UUID,
    body: ReviewDecisionRequest | None = None,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _decide(
        request,
        conn,
        project,
        candidate_id,
        "approve",
        "approveMergeCandidate",
        body,
        idempotency_key,
    )


@router.post("/projects/{project}/merge-candidates/{candidate_id}/reject")
async def reject_merge_candidate_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    candidate_id: uuid.UUID,
    body: ReviewDecisionRequest | None = None,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _decide(
        request,
        conn,
        project,
        candidate_id,
        "reject",
        "rejectMergeCandidate",
        body,
        idempotency_key,
    )


@router.post("/projects/{project}/merge-candidates/{candidate_id}/defer")
async def defer_merge_candidate_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    candidate_id: uuid.UUID,
    body: ReviewDecisionRequest | None = None,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _decide(
        request,
        conn,
        project,
        candidate_id,
        "defer",
        "deferMergeCandidate",
        body,
        idempotency_key,
    )
