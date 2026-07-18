"""Review endpoints (BA5) — the Console's §17 merge-candidate queue.

List serves the ACTIVE build's candidates through the DR-006 repo (the BA3
binding/cursor pattern verbatim) — the review queue by default, any single
§17 status via ``filter[status]`` (GOV4); the three decision verbs call
``core.resolve.decisions.decide_merge_candidate``, whose single transaction
locks the candidate, validates the §17 transition, writes the DR-003
carry-forward ledger entry (keyed by the same v2 ``ledger_merge_key``
resolve reads — including for DEFER, which must block a future auto-merge),
and stamps the audit trail. Idempotency-Key rides the BA1b machinery with every precheck
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
from api.routers._query import reject_null_body, reject_unsupported_query, single_filter_value
from api.schemas import ReviewDecisionRequest, merge_candidate_dto, ontology_proposal_dto
from core.graph.proposals import (
    InvalidProposalTransitionError,
    OntologyProposalNotFoundError,
    decide_ontology_proposal,
    list_ontology_proposals,
)
from core.registry import ProjectNotFoundError, get_project
from core.resolve.decisions import (
    InvalidReviewTransitionError,
    MergeCandidateNotFoundError,
    decide_merge_candidate,
)
from core.resolve.review import STATE_MACHINES
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


def _status_filter(request: Request) -> str | None:
    """The validated ``filter[status]`` value, or None when absent (GOV4).

    The vocabulary is §17's merge-candidate state machine — read from
    ``STATE_MACHINES`` itself, not a restated literal set, so the filter can
    never drift from what decisions may actually write. Single-value and
    vocabulary rules live in the shared helper (SS1a: one implementation for
    every facet endpoint, class 5)."""
    return single_filter_value(
        request, "status", vocabulary=tuple(STATE_MACHINES["merge_candidate"])
    )


@router.get("/projects/{project}/merge-candidates")
async def list_merge_candidates_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id", allowed_filters=frozenset({"status"}))
    status_filter = _status_filter(request)
    binding = await _bind(conn, project)
    repo = BuildScopedRepo.bound_to(conn, binding)
    mc = tables.merge_candidates
    # the DEFAULT list IS the review queue: only still-reviewable candidates
    # appear — the same pending+deferred definition §19's pending_review
    # metric counts (core/observability/health.py), so the queue and its
    # gauge never diverge (Codex #59 R1). An explicit filter[status] (GOV4)
    # is the audit surface over the same SoR: decided rows become listable
    # only when the consumer names the status it wants.
    where: list[Any] = (
        [mc.c.status == status_filter]
        if status_filter is not None
        else [mc.c.status.in_(("pending", "deferred"))]
    )
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


# --- GOV3: ontology proposal pool (§17 proposed → accepted|rejected) ----------


async def _require_project(conn: Any, project: str) -> None:
    """A pool listing under a missing project is a 404, not an empty 200 (the
    proposal pool is NOT build-scoped, so there is no active binding to fail on
    — the project's existence is the only precondition)."""
    if await get_project(conn, project) is None:
        raise translate_registry_error(ProjectNotFoundError(project))


def _proposal_status_filter(request: Request) -> str | None:
    """The validated ``filter[status]``, read from the §17 ontology-proposal
    machine itself (never a restated literal) so it can't drift from what a
    decision may write."""
    return single_filter_value(
        request, "status", vocabulary=tuple(STATE_MACHINES["ontology_proposal"])
    )


@router.get("/projects/{project}/ontology-proposals")
async def list_ontology_proposals_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    reject_unsupported_query(request, "id", allowed_filters=frozenset({"status"}))
    status_filter = _proposal_status_filter(request)
    await _require_project(conn, project)
    after = decode_id_cursor(cursor)[0] if cursor else None
    proposals, next_after = await list_ontology_proposals(
        conn, project, limit=limit, after=after, status=status_filter
    )
    return success(
        [ontology_proposal_dto(p) for p in proposals],
        **response_meta(request),
        paginated=True,
        next_cursor=encode_cursor((next_after,)) if next_after is not None else None,
    )


async def _decide_proposal(
    request: Request,
    conn: Any,
    project: str,
    proposal_id: uuid.UUID,
    verb: str,
    endpoint: str,
    body: ReviewDecisionRequest | None,
    idempotency_key: str | None,
) -> JSONResponse:
    await reject_null_body(request)  # optional body, non-nullable when present (#53 R5)
    reason = body.reason if body is not None else None

    async def produce() -> tuple[int, dict[str, Any]]:
        # every precheck INSIDE produce (#53 R2): a replayed decision must win
        # even if the project has since vanished
        try:
            proposal = await decide_ontology_proposal(
                conn,
                project=project,
                proposal_id=proposal_id,
                verb=verb,
                decided_by=_CONSOLE_DECIDER,
                reason=reason,
            )
        except (ProjectNotFoundError, OntologyProposalNotFoundError) as exc:
            raise translate_registry_error(exc) from exc
        except InvalidProposalTransitionError as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,  # GAP: no frozen review-conflict code
                str(exc),
                details={"status": exc.current, "decision": exc.verb},
            ) from exc
        return 200, success(ontology_proposal_dto(proposal), **response_meta(request))

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


@router.post("/projects/{project}/ontology-proposals/{proposal_id}/accept")
async def accept_ontology_proposal_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    proposal_id: uuid.UUID,
    body: ReviewDecisionRequest | None = None,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _decide_proposal(
        request,
        conn,
        project,
        proposal_id,
        "accept",
        "acceptOntologyProposal",
        body,
        idempotency_key,
    )


@router.post("/projects/{project}/ontology-proposals/{proposal_id}/reject")
async def reject_ontology_proposal_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    proposal_id: uuid.UUID,
    body: ReviewDecisionRequest | None = None,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    return await _decide_proposal(
        request,
        conn,
        project,
        proposal_id,
        "reject",
        "rejectOntologyProposal",
        body,
        idempotency_key,
    )
