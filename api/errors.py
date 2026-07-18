"""The frozen error vocabulary + the one exception every handler raises (§15/§27.2).

DR-002: ``ErrorCode`` mirrors the frozen ``contracts/openapi.yaml`` enum
EXACTLY, and ``_HTTP_STATUS`` mirrors the status→code mapping the contract
documents in prose (the ``responses.Error`` description). BOTH are locked in
lockstep by contract tests — a code or a status that drifts from the frozen
artifact fails CI rather than reaching a client. The contract is the source
of truth (DR-002); this module conforms to it, it does not decide it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """§27.2's frozen, additive-only error codes. Value == the contract
    string; membership is asserted equal to the contract enum in tests."""

    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    BUILD_NOT_FOUND = "BUILD_NOT_FOUND"
    BUILD_NOT_READY = "BUILD_NOT_READY"
    NO_ACTIVE_BUILD = "NO_ACTIVE_BUILD"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_CONFLICT = "JOB_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUERY_UNSAFE = "QUERY_UNSAFE"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"
    # v1.3 (DR-013, additive): precise not-found codes for the new source/
    # proposal surfaces + the retry state refusal (mirrors BUILD_NOT_READY).
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    BUILD_NOT_RETRYABLE = "BUILD_NOT_RETRYABLE"


#: code → HTTP status. Every ErrorCode MUST appear (a lockstep test asserts
#: total coverage — an unmapped code would fall through to 500 and mislabel
#: a 404 as a server fault).
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PROJECT_NOT_FOUND: 404,
    ErrorCode.BUILD_NOT_FOUND: 404,
    ErrorCode.BUILD_NOT_READY: 409,
    ErrorCode.NO_ACTIVE_BUILD: 409,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.JOB_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.QUERY_UNSAFE: 400,
    ErrorCode.QUERY_TIMEOUT: 504,
    ErrorCode.STORE_UNAVAILABLE: 503,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.SOURCE_NOT_FOUND: 404,
    ErrorCode.PROPOSAL_NOT_FOUND: 404,
    ErrorCode.BUILD_NOT_RETRYABLE: 409,
}


def http_status_for(code: ErrorCode) -> int:
    """The HTTP status for a frozen error code (total over ErrorCode)."""
    return _HTTP_STATUS[code]


#: HTTP status → the frozen code, ONLY where the contract maps that status to
#: exactly one code (429→RATE_LIMITED, 503→STORE_UNAVAILABLE,
#: 504→QUERY_TIMEOUT, 500→INTERNAL). Ambiguous statuses (400/404/409 carry
#: several codes) are omitted — a bare framework error at those can't pick one.
_UNIQUE_STATUS_CODE: dict[int, ErrorCode] = {
    status: next(c for c in ErrorCode if _HTTP_STATUS[c] == status)
    for status in {s for s in _HTTP_STATUS.values()}
    if sum(1 for c in ErrorCode if _HTTP_STATUS[c] == status) == 1
}


def code_for_framework_status(status: int) -> ErrorCode:
    """The frozen code for a framework-raised HTTPException at ``status``:
    the contract's code when the status determines exactly one (so a client
    dispatching on ``error.code`` sees the class the status promises — e.g.
    503→STORE_UNAVAILABLE), else a coarse classification (4xx = the client's
    request didn't conform → VALIDATION_ERROR; 5xx = server fault →
    INTERNAL)."""
    if status in _UNIQUE_STATUS_CODE:
        return _UNIQUE_STATUS_CODE[status]
    return ErrorCode.INTERNAL if status >= 500 else ErrorCode.VALIDATION_ERROR


class ApiError(Exception):
    """The single exception the API raises; the app's handler renders it as
    the frozen error envelope. Carrying the CODE (not an HTTP status) keeps
    call sites speaking the domain vocabulary — the status is derived."""

    def __init__(
        self, code: ErrorCode, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def http_status(self) -> int:
        return http_status_for(self.code)
