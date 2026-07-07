"""Shared list-query handling for the registry routers (BA1b).

BA1a's ``list_*`` support only the default keyset order (created_at/added_at
desc), and the opaque cursor is bound to it. The frozen op params still expose
``sort``/``filter``, so rather than silently ignore them (which would mislead a
client into thinking they took effect), we accept only the default sort and
reject everything else — and any ``filter[...]`` — as VALIDATION_ERROR. Broader
sort/filter is a future item that extends the registry.
"""

from __future__ import annotations

from fastapi import Request

from api.errors import ApiError, ErrorCode


def reject_unsupported_query(request: Request, sort_field: str) -> None:
    """Reject a non-default ``sort`` or any ``filter[...]`` — BA1b supports
    only the default keyset order for ``sort_field`` (desc)."""
    if any(k.startswith("filter[") for k in request.query_params):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "filtering is not supported yet",
            details={"filter": "unsupported"},
        )
    sort = request.query_params.get("sort")
    if sort is not None and sort != f"{sort_field}:desc":
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"only sort={sort_field}:desc is supported",
            details={"sort": sort},
        )
