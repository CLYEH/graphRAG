"""The §15 response envelopes — the ONE place the wire shape is built.

Success: ``{"data": <payload>, "meta": {request_id, build_id, elapsed_ms}}``.
Error:   ``{"error": {code, message, details, request_id}}`` — details is
null rather than absent (the frozen Error shape is always fully present, so
consumers never branch on a missing field). Paginated lists add
``next_cursor`` to meta. Every field here traces to a frozen contract
schema (Meta / PageMeta / Error); a drift test compares against them.
"""

from __future__ import annotations

import uuid
from typing import Any

from api.errors import ApiError, ErrorCode


def success(
    data: Any,
    *,
    request_id: uuid.UUID,
    elapsed_ms: int,
    build_id: uuid.UUID | None = None,
    next_cursor: str | None = None,
    paginated: bool = False,
    total: int | None = None,
    total_estimated: bool = False,
) -> dict[str, Any]:
    """Wrap a payload in the §15 success envelope. ``paginated`` adds the
    required ``next_cursor`` to meta (null on the last page) — a list
    response MUST use it so clients always distinguish the last page from a
    non-conforming body.

    ``total`` (SS1b, DR-013): the count of matching rows. Emitted ONLY when the
    endpoint computed one — the PageMeta field is optional and nullable, so a
    list that does not compute a total simply omits it (clients read absent as
    "unknown", never zero). ``total_estimated`` rides along only when a total is
    present; false means the count is exact (the current exact-count path — a
    planner estimate for large tables is the deferred indexing follow-up)."""
    meta: dict[str, Any] = {
        "request_id": str(request_id),
        "build_id": str(build_id) if build_id is not None else None,
        "elapsed_ms": elapsed_ms,
    }
    if paginated:
        meta["next_cursor"] = next_cursor
    if total is not None:
        meta["total"] = total
        meta["total_estimated"] = total_estimated
    return {"data": data, "meta": meta}


def error_body(
    code: ErrorCode, message: str, *, request_id: uuid.UUID, details: dict[str, Any] | None
) -> dict[str, Any]:
    """The §15 error envelope. ``details`` is emitted as null, never
    omitted (the frozen Error requires the key present)."""
    return {
        "error": {
            "code": code.value,
            "message": message,
            "details": details,
            "request_id": str(request_id),
        }
    }


def error_body_from(exc: ApiError, *, request_id: uuid.UUID) -> dict[str, Any]:
    return error_body(exc.code, exc.message, request_id=request_id, details=exc.details)
