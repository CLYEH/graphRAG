"""Shared list-query handling for the registry/inspect routers (BA1b/BA3).

The ``list_*`` reads support only their default keyset order, and the opaque
cursor is bound to it. The frozen op params still expose ``sort``/``filter``,
so rather than silently ignore them (which would mislead a client into
thinking they took effect), we accept only the default sort and reject
everything else — and any ``filter[...]`` — as VALIDATION_ERROR. Broader
sort/filter is a future item that extends the reads.
"""

from __future__ import annotations

from fastapi import Request

from api.errors import ApiError, ErrorCode


def reject_unsupported_query(request: Request, sort_field: str | None) -> None:
    """Reject a non-default ``sort`` or any ``filter[...]``. ``sort_field``
    names the single-column desc default an explicit ``sort`` may restate
    (BA1b lists); None means the default order is compound (e.g. chunks'
    (document_id, ordinal)) and NO explicit sort can restate it — every
    ``sort`` is rejected rather than half-matched."""
    if any(k.startswith("filter[") for k in request.query_params):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "filtering is not supported yet",
            details={"filter": "unsupported"},
        )
    sort = request.query_params.get("sort")
    if sort is None:
        return
    if sort_field is None or sort != f"{sort_field}:desc":
        supported = (
            f"only sort={sort_field}:desc is supported"
            if sort_field
            else ("explicit sort is not supported on this list")
        )
        raise ApiError(ErrorCode.VALIDATION_ERROR, supported, details={"sort": sort})
