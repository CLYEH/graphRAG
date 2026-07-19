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
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from neo4j.exceptions import DriverError, Neo4jError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncConnection

from api.deps import Conn, Queue, neo4j_driver, qdrant_client, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.pagination import decode_id_cursor, decode_step_cursor, encode_cursor
from api.registry_errors import translate_registry_error
from api.routers._query import reject_null_body, reject_unsupported_query, single_filter_value
from api.schemas import (
    RetryRequest,
    build_dto,
    build_step_dto,
    build_step_item_dto,
    job_accepted_dto,
)
from api.workers.build_worker import RETRY_JOB_KIND, enqueue_build, enqueue_eval
from core.builds.creation import create_build
from core.builds.lifecycle import (
    BuildInfo,
    activate_in_caller_txn,
    get_build_info,
    list_builds_page,
)
from core.builds.retry import clone_raw_artifacts
from core.config import get_settings
from core.eval.idempotency import eval_inputs_fingerprint, policy_fingerprint_bytes
from core.observability.reads import (
    list_build_steps,
    list_step_items,
    step_belongs_to_build,
)
from core.registry import (
    EVAL_JOB_KIND,
    JobConflictError,
    ProjectNotFoundError,
    build_config_snapshot,
    create_job_exclusive,
    get_project,
    set_eval_inputs_fingerprint,
)

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


@router.get("/projects/{project}/builds/{build_id}/steps")
async def list_build_steps_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    build_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    # RB1 §27.7 drill-down: the build's pipeline steps. `status` is an OPEN
    # vocabulary (no DDL CHECK — the C2–C7 writers own it), so the facet
    # validates blankness only. The order is COMPOUND (newest run first =
    # run started_at desc, step id desc), so — like chunks' compound order — NO
    # explicit sort can restate it: sort_field=None rejects every sort loud
    # rather than 200 an `id:desc` the endpoint does not actually honor (Codex
    # #99 R2 / the GAPS-O4 false-affordance rule).
    reject_unsupported_query(request, None, allowed_filters=frozenset({"status"}))
    status = single_filter_value(request, "status")
    await _require_build(conn, project, build_id)  # project 404 then build 404
    after = decode_step_cursor(cursor) if cursor else None
    steps, next_after = await list_build_steps(
        conn, project, build_id, limit=limit, after=after, status=status
    )
    return success(
        [build_step_dto(s) for s in steps],
        **response_meta(request),
        build_id=build_id,
        paginated=True,
        next_cursor=encode_cursor(next_after) if next_after is not None else None,
    )


@router.get("/projects/{project}/builds/{build_id}/steps/{step_id}/items")
async def list_step_items_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    build_id: uuid.UUID,
    step_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    # RB1 §27.7 drill-down: one step's recorded item outcomes. item.status is an
    # OPEN vocabulary (failed/skipped are the frozen minimum, but sampled/all
    # verbosity records successes too) — blankness-validated only.
    reject_unsupported_query(request, "id", allowed_filters=frozenset({"status"}))
    status = single_filter_value(request, "status")
    await _require_build(conn, project, build_id)  # project 404 then build 404
    if not await step_belongs_to_build(conn, project, build_id, step_id):
        # GAP: no frozen step-not-found code (the build exists, the step does
        # not) — a true 404 with the coarse code, exactly as inspect handles a
        # missing document/chunk/entity under a valid build.
        raise HTTPException(status_code=404, detail=f"step {step_id} not found in build {build_id}")
    after = decode_id_cursor(cursor)[0] if cursor else None
    items, next_after = await list_step_items(
        conn, project, build_id, step_id, limit=limit, after=after, status=status
    )
    return success(
        [build_step_item_dto(i) for i in items],
        **response_meta(request),
        build_id=build_id,
        paginated=True,
        next_cursor=encode_cursor((next_after,)) if next_after is not None else None,
    )


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


@router.post("/projects/{project}/builds/{build_id}/eval")
async def run_build_eval_endpoint(
    request: Request,
    conn: Conn,
    get_redis: Queue,
    project: str,
    build_id: uuid.UUID,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    """Run the project's golden set against the NAMED build as an async job
    (UXC1b, DR-010) — the same core path the CLI eval walks; the report lands in
    ``builds.eval`` where the activation gate already reads §14 scores, so
    Console gating gets zero new coupling. Mirrors the trigger endpoints: one
    active job per project (409 ``JOB_CONFLICT``), enqueue IN-BAND before commit
    (the class-12 window), 202 + the job envelope, watchable via
    ``GET /jobs/{id}/events``. The build is named in the path (no request body);
    a bad/unready build is a REFUSAL the eval job records, not a synchronous
    gate — the CLI path refuses the same way.

    Idempotency is per (build, golden-set fingerprint) per the frozen contract: the
    build is in the path, and the golden set / query policy content is folded into
    the request hash (``eval_inputs_fingerprint``), so reusing an ``Idempotency-Key``
    after the golden set changes within the TTL does NOT replay a run scored against
    the stale inputs — a changed fingerprint is the §27 key-reused-with-a-different-
    request conflict (client uses a fresh key), never a silent stale replay."""

    # ONE accepted fingerprint (Codex #93 R7, completing R2's split). The
    # UNLOCKED read below still feeds the INITIAL request hash — it must exist
    # before produce (a §27 replay/conflict is decided without running it).
    # But the identity the record KEEPS is finalized inside produce: the
    # fingerprint is recomputed UNDER the projects-row lock (taken by
    # get_project for_update=True, the same lock create_job_exclusive re-takes
    # and the config PATCH serializes on — idem-row → projects-row order,
    # matching the trigger endpoints, no lock inversion), the job pin gets
    # that value, and run_idempotent's rekey stores the hash DERIVED FROM IT.
    # Record ≡ pin, so a PATCH committing between the unlocked read and the
    # lock can no longer make the record claim an identity the job wasn't
    # scored under (retry-under-the-accepted-policy would 409 instead of
    # replaying; a policy flipped BACK would silently replay a job scored
    # against different inputs).
    async def _policy_bytes(*, for_update: bool = False) -> bytes:
        registry_row = await get_project(conn, project, for_update=for_update)
        config = (
            registry_row.config
            if registry_row is not None and isinstance(registry_row.config, dict)
            else {}
        )
        return policy_fingerprint_bytes(config)

    raw_body = await request.body()

    def _req_hash(fingerprint: str) -> str:
        # per (build, golden-set fingerprint): the build_id is in request.url.path;
        # the golden set + query policy content is folded as the "body" so a changed
        # golden set flips the hash (no stale replay). The ACTUAL request body is
        # folded too — the endpoint is bodyless, but FastAPI still accepts one, and
        # the sibling bodyless endpoints (rollback) hash await request.body(); a
        # stray/different body on a reused key must be a §27 conflict, not a silent
        # replay. The fingerprint (a hex digest / sentinel, never containing \0)
        # leads, then a \0 delimiter, then the raw body — an unambiguous split, so
        # no (fingerprint, body) pair can alias another.
        return request_hash("POST", request.url.path, fingerprint.encode() + b"\0" + raw_body)

    fingerprint = eval_inputs_fingerprint(
        Path(get_settings().projects_dir), project, await _policy_bytes()
    )
    accepted: dict[str, str] = {}

    async def produce() -> tuple[int, dict[str, Any]]:
        # lock FIRST, then fingerprint: everything below sees one frozen config
        locked_fingerprint = eval_inputs_fingerprint(
            Path(get_settings().projects_dir), project, await _policy_bytes(for_update=True)
        )
        try:
            job = await create_job_exclusive(conn, project, EVAL_JOB_KIND, build_id=build_id)
        except (ProjectNotFoundError, JobConflictError) as exc:
            raise translate_registry_error(exc) from exc
        # the pin AND the kept §27 identity: the SAME under-lock value (atomic
        # with the job insert, consistent with what dispatch will read)
        await set_eval_inputs_fingerprint(conn, job.id, locked_fingerprint)
        accepted["hash"] = _req_hash(locked_fingerprint)
        # queue touched HERE only — a §27 replay or a 409 must be served even
        # with Redis unreachable (the Queue dep is a lazy handle), same as _trigger
        await enqueue_eval(await get_redis(), project, job.id, build_id)
        return 202, success(job_accepted_dto(job), **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=project,
            endpoint="runBuildEval",
            req_hash=_req_hash(fingerprint),
            produce=produce,
            # the record keeps the ACCEPTED identity (see the block comment)
            rekey=lambda: accepted.get("hash"),
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))


@router.post("/projects/{project}/builds/{build_id}/retry")
async def retry_build_endpoint(
    request: Request,
    conn: Conn,
    get_redis: Queue,
    project: str,
    build_id: uuid.UUID,
    body: RetryRequest | None = None,  # shape-validates; reason rejects loudly
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    """Retry a terminal ``failed`` build's failed items as a NEW build (RB1-retry,
    DR-013). Opens a child build recording ``parent_build_id``, clones the
    parent's documents into it, and dispatches it as a build job (kind ``retry``)
    — the worker resumes the ready-seeded child, so the §5 pipeline reruns and
    converges. The parent's terminal record is never mutated (audit integrity —
    lineage is the child's pointer). Only a terminal ``failed`` build is
    retryable (any other status → 409 ``BUILD_NOT_RETRYABLE``), and a failed
    build that committed no documents (it failed at/before ingest) is equally
    not retryable — there is nothing to reuse; a full re-run is ``POST /build``.

    Mirrors the trigger/eval endpoints: one active job per project (409
    ``JOB_CONFLICT``), enqueue IN-BAND before the request commits (the class-12
    window), 202 + the job envelope, watchable via ``GET /jobs/{id}/events``.
    The child build, the job insert, and the document clone share the request's
    ONE transaction; the exclusive-job guard is taken BEFORE the clone, so a
    ``JOB_CONFLICT`` rolls back just the child row (never a full corpus copy) and
    leaves no orphan ``building`` build stranded without a job.

    NOTE (RB1-retry-core): the child reruns EVERY stage and converges — the
    per-item compute-skip that reruns ONLY the failed items (and reuses the
    parent's graph layer without re-extraction) is the deferred RB1-retry-skip
    slice. Cloning only documents (not chunks/graph) is deliberate: ``clean``
    re-chunks fresh (cloned chunks would risk InconsistentChunksError on a
    chunk-config change) and re-running graph over cloned entities would
    drift/grow them (see core.builds.retry).
    """
    # optional body, non-nullable when present (#53 R5 class — the shared guard
    # every sibling optional-body POST uses; a literal `null` body must 400, not
    # bind to None and silently proceed, which would contradict the field-null
    # rejection RetryRequest already does on `reason`)
    await reject_null_body(request)

    async def produce() -> tuple[int, dict[str, Any]]:
        # Every precheck INSIDE produce (#53 R2 ordering rule): a replayed retry
        # must return its stored 202 even if the parent build has since been
        # pruned (idempotency_keys survive project churn) — a FRESH request still
        # gets the synchronous 404/409 here (Codex #100 P2 R2).
        parent = await _require_build(conn, project, build_id)
        if parent.status != "failed":
            raise ApiError(
                ErrorCode.BUILD_NOT_RETRYABLE,
                f"build {build_id} is {parent.status!r}, not a terminal 'failed' build; "
                "only a failed build can be retried",
                details={"build_id": str(build_id), "status": parent.status},
            )
        # Serialize concurrent same-project retries on the projects row BEFORE
        # create_build takes its FK FOR KEY SHARE: two retries could otherwise
        # each hold FOR KEY SHARE and then deadlock competing for
        # create_job_exclusive's FOR UPDATE (a 500 instead of a clean 409). This
        # is the SAME lock create_job_exclusive re-takes, so the loser blocks
        # here, then finds the winner's active job and gets JOB_CONFLICT — the
        # lock order _trigger already uses (projects-row FOR UPDATE first).
        await get_project(conn, project, for_update=True)
        # RB1-retry-skip (owner 2026-07-19: 凍語料完備): the child must re-process
        # under the PARENT's config, not the current project config. Its graph
        # stage reuses the parent's (cloned, parent-config) graph layer and
        # re-extracts only the failed docs; if a later PATCH /projects drifted the
        # chunking/ontology, running the re-extraction under the CURRENT config
        # would mix two configs in one build (and the cloned text refs would no
        # longer line up with the child's re-chunk). Pin the parent's snapshot onto
        # the retry job (None → the parent predates the pin → live-config fallback,
        # i.e. retry-core's behavior).
        parent_config = await build_config_snapshot(conn, build_id)
        try:
            child_build_id = await create_build(conn, project, parent_build_id=build_id)
            # Take the single-active-job guard BEFORE the (potentially large)
            # clone, so a JOB_CONFLICT — or the loser of two concurrent retries —
            # rolls back just the child row, never a full corpus INSERT..SELECT
            # (Codex #100 P2 R1). Both create_build (project vanished under us)
            # and create_job_exclusive map their registry errors through the same
            # translator (symmetric 404/409); the whole txn is atomic (no orphan).
            job = await create_job_exclusive(
                conn,
                project,
                RETRY_JOB_KIND,
                build_id=child_build_id,
                config_snapshot=parent_config,
            )
            clone = await clone_raw_artifacts(conn, project, build_id, child_build_id)
            # A parent that failed AT/BEFORE ingest committed no documents (the
            # ingest stage is one all-or-nothing txn), so there is nothing to
            # reuse — and because a retry child SKIPS live-source ingest, letting
            # it through would run the pipeline on an EMPTY corpus and reach
            # 'ready', silently masking the ingest failure (Codex #100 P1 R2).
            # Refuse instead; the ApiError rolls the child + job + clone back.
            if clone.documents == 0:
                raise ApiError(
                    ErrorCode.BUILD_NOT_RETRYABLE,
                    f"build {build_id} committed no documents (it failed at or before "
                    "ingest), so there is nothing to reuse; fix the sources and run a "
                    "full build (POST /build)",
                    details={"build_id": str(build_id), "documents": 0},
                )
        except (ProjectNotFoundError, JobConflictError) as exc:
            raise translate_registry_error(exc) from exc
        # queue touched HERE only — a §27 replay or a 409 must be served even
        # with Redis unreachable (the Queue dep is a lazy handle), same as
        # _trigger. kind 'retry' rides the BUILD_TASK path via enqueue_build; the
        # worker resumes child_build_id (already 'building', raw layer cloned).
        await enqueue_build(await get_redis(), project, job.id)
        return 202, success(job_accepted_dto(job), **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=project,
            endpoint="retryBuild",
            req_hash=request_hash("POST", request.url.path, await request.body()),
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))
