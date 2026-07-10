"""FastAPI application skeleton (BA0) — the app every Track-2 router mounts on.

What BA0 establishes and later BA-items build on:
- **The published contract IS the frozen artifact.** ``app.openapi()`` returns
  ``contracts/openapi.yaml`` verbatim (DR-002: contracts/ is the source of
  truth; web generates its client from this). Routers added by BA1+ are
  checked FOR conformance against it — the served schema is never
  re-generated from code and allowed to drift.
- **One request_id per request**, stamped on ``request.state`` by middleware
  and echoed in every envelope (success meta and error) + the
  ``X-Request-ID`` header.
- **Every error is the frozen envelope**: ApiError → its mapped status +
  ``{"error": {...}}``; request-body validation → VALIDATION_ERROR (400, the
  contract's mapping — not FastAPI's default 422); framework HTTPExceptions
  (unknown route, wrong method) → the envelope with the true status and the
  contract's code where the status determines one (503 → STORE_UNAVAILABLE),
  else a coarse 4xx→VALIDATION_ERROR / 5xx→INTERNAL — instead of Starlette's
  ``{"detail": …}``; anything uncaught → INTERNAL (500). No FastAPI/Starlette
  default error shape ever reaches a client.

BA0 mounts NO domain routes (those are BA1–BA8) — a skeleton with the
cross-cutting machinery wired and tested.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from api.deps import lifespan
from api.envelope import error_body, error_body_from
from api.errors import ApiError, ErrorCode, code_for_framework_status, http_status_for
from api.routers import inspect as inspect_router
from api.routers import jobs as jobs_router
from api.routers import projects as projects_router
from api.routers import sources as sources_router
from api.routers import triggers as triggers_router

#: source checkout keeps contracts/ at the repo root; an installed wheel
#: ships the build-time copy inside core/ (pyproject force-include) — same
#: two-candidate resolution the eval schema loader uses.
_CONTRACT_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "contracts" / "openapi.yaml",
    Path(__file__).resolve().parent.parent / "core" / "contracts" / "openapi.yaml",
)


@lru_cache(maxsize=1)
def _frozen_contract() -> dict[str, Any]:
    for candidate in _CONTRACT_CANDIDATES:
        if candidate.exists():
            with candidate.open(encoding="utf-8") as handle:
                loaded: dict[str, Any] = yaml.safe_load(handle)
            return loaded
    raise FileNotFoundError(
        f"frozen OpenAPI contract not found (looked in {[str(c) for c in _CONTRACT_CANDIDATES]})"
    )


def _request_id(request: Request) -> uuid.UUID:
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, uuid.UUID) else uuid.uuid4()


class _RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamp a request_id and start clock on ``request.state`` before the
    route runs, and surface the id as ``X-Request-ID`` (so a client can
    correlate even a response whose body it failed to parse)."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        request.state.request_id = uuid.uuid4()
        request.state.start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="graphRAG Console API", version="1.0", lifespan=lifespan)
    app.add_middleware(_RequestContextMiddleware)

    def _error_response(
        status: int, body: dict[str, Any], *, extra_headers: Mapping[str, str] | None = None
    ) -> JSONResponse:
        # exception handlers run INSIDE Starlette's ServerErrorMiddleware, so
        # the request-context middleware never sees these responses — stamp
        # X-Request-ID here too, or a 4xx/5xx would ship without the header.
        # jsonable_encoder on the WHOLE body: ApiError.details can hold UUIDs
        # or datetimes (build/job ids), which JSONResponse can't serialize —
        # unencoded, the contract-mapped error would crash into the 500 path.
        # extra_headers carries framework protocol hints (405 Allow,
        # WWW-Authenticate, Retry-After); X-Request-ID is always ours last.
        rid = body["error"]["request_id"]
        headers = {**extra_headers, "X-Request-ID": rid} if extra_headers else {"X-Request-ID": rid}
        return JSONResponse(status_code=status, content=jsonable_encoder(body), headers=headers)

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(
            exc.http_status, error_body_from(exc, request_id=_request_id(request))
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # the contract maps VALIDATION_ERROR → 400 (not FastAPI's default 422).
        # errors() can hold non-serializable objects (a custom validator's ctx
        # carries the original ValueError); _error_response encodes the whole
        # body, so raw errors() is safe here.
        return _error_response(
            http_status_for(ErrorCode.VALIDATION_ERROR),
            error_body(
                ErrorCode.VALIDATION_ERROR,
                "request validation failed",
                request_id=_request_id(request),
                details={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # framework HTTPExceptions (unknown route, wrong method, or a raised
        # HTTPException) must wear the frozen envelope too, never Starlette's
        # {"detail": ...}. The code is the contract's code when the status
        # determines one uniquely (503→STORE_UNAVAILABLE, so a client
        # dispatching on error.code sees the class the status promises), else
        # a coarse 4xx/5xx classification — while PRESERVING the true status.
        # Domain handlers (BA1+) raise precise ApiErrors.
        # 5xx: fixed message (the code name) — exc.detail may carry
        # backend/downstream failure info that must not leak on a server
        # fault. 4xx: echo the framework's detail (client-facing, e.g. "Not
        # Found"), falling back to the code name.
        code = code_for_framework_status(exc.status_code)
        if exc.status_code >= 500:
            message = code.value
        else:
            message = str(exc.detail) if exc.detail else code.value
        return _error_response(
            exc.status_code,
            error_body(code, message, request_id=_request_id(request), details=None),
            # keep protocol hints the framework attached (405 Allow, etc.)
            extra_headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _handle_uncaught(request: Request, exc: Exception) -> JSONResponse:
        # never leak an internal message or a stack — a fixed INTERNAL body
        return _error_response(
            http_status_for(ErrorCode.INTERNAL),
            error_body(
                ErrorCode.INTERNAL, "internal error", request_id=_request_id(request), details=None
            ),
        )

    def _openapi() -> dict[str, Any]:
        # the frozen contract IS the published schema (DR-002) — not a
        # code-generated one that could drift from contracts/openapi.yaml
        app.openapi_schema = _frozen_contract()
        return app.openapi_schema

    app.openapi = _openapi  # type: ignore[method-assign]

    # Domain routers (BA1b+). The served /openapi.json stays the frozen
    # contract regardless of what's mounted (DR-002, _openapi above).
    app.include_router(projects_router.router)
    app.include_router(sources_router.router)
    app.include_router(triggers_router.router)
    app.include_router(jobs_router.router)
    app.include_router(inspect_router.router)
    return app
