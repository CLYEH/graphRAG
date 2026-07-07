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
  contract's mapping — not FastAPI's default 422); anything uncaught →
  INTERNAL (500). FastAPI's default HTML/JSON error shapes never reach a
  client.

BA0 mounts NO domain routes (those are BA1–BA8) — a skeleton with the
cross-cutting machinery wired and tested.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.envelope import error_body, error_body_from
from api.errors import ApiError, ErrorCode, http_status_for

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
    app = FastAPI(title="graphRAG Console API", version="1.0")
    app.add_middleware(_RequestContextMiddleware)

    def _error_response(status: int, body: dict[str, Any]) -> JSONResponse:
        # exception handlers run INSIDE Starlette's ServerErrorMiddleware, so
        # the request-context middleware never sees these responses — stamp
        # X-Request-ID here too, or a 4xx/5xx would ship without the header
        rid = body["error"]["request_id"]
        return JSONResponse(status_code=status, content=body, headers={"X-Request-ID": rid})

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(
            exc.http_status, error_body_from(exc, request_id=_request_id(request))
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # the contract maps VALIDATION_ERROR → 400 (not FastAPI's default 422)
        return _error_response(
            http_status_for(ErrorCode.VALIDATION_ERROR),
            error_body(
                ErrorCode.VALIDATION_ERROR,
                "request validation failed",
                request_id=_request_id(request),
                details={"errors": exc.errors()},
            ),
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
    return app
