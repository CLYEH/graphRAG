"""Shared list-query handling for the registry/inspect routers (BA1b/BA3).

The ``list_*`` reads support only their default keyset order, and the opaque
cursor is bound to it. The frozen op params still expose ``sort``/``filter``,
so rather than silently ignore them (which would mislead a client into
thinking they took effect), we accept only the default sort and reject
everything else — and any ``filter[...]`` outside the endpoint's explicit
``allowed_filters`` — as VALIDATION_ERROR (GAPS O4: a 200 that pretends a
filter took effect misleads the consumer). Endpoints that implement a facet
name it in ``allowed_filters`` and read the value themselves.
"""

from __future__ import annotations

from fastapi import Request

from api.errors import ApiError, ErrorCode


def reject_unsupported_query(
    request: Request,
    sort_field: str | None,
    allowed_filters: frozenset[str] = frozenset(),
) -> None:
    """Reject a non-default ``sort`` or any unsupported ``filter``.
    ``sort_field`` names the single-column desc default an explicit ``sort``
    may restate (BA1b lists); None means the default order is compound (e.g.
    chunks' (document_id, ordinal)) and NO explicit sort can restate it —
    every ``sort`` is rejected rather than half-matched. ``allowed_filters``
    names the deepObject fields the calling endpoint actually implements;
    everything else — a field outside the set, a malformed bracket spelling,
    or the bare non-deepObject ``filter=...`` (which used to pass the
    ``filter[`` prefix check unseen and silently no-op, GAPS O4's evidence)
    — is rejected, never ignored."""
    for key in request.query_params:
        if key != "filter" and not key.startswith("filter["):
            continue
        well_formed = key.startswith("filter[") and key.endswith("]")
        field = key[len("filter[") : -1] if well_formed else None
        if field is not None and field in allowed_filters:
            continue
        supported = (
            f"supported filters: {', '.join(f'filter[{f}]' for f in sorted(allowed_filters))}"
            if allowed_filters
            else "filtering is not supported on this list"
        )
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{supported}; filters are spelled filter[field]=value",
            details={"filter": key},
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


async def reject_null_body(request: Request) -> None:
    """Reject an explicit JSON ``null`` request body (400) on endpoints whose
    body is OPTIONAL but non-nullable when present — FastAPI binds `null` to
    None, indistinguishable from absent, which would silently run the
    operation for a contract-invalid request (the #53 R5 class; extracted
    from the trigger router when the review decide endpoints grew the same
    optional-body shape)."""
    if (await request.body()).strip(b" \t\r\n") == b"null":
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "request body may not be JSON null; omit the body instead",
        )


def single_filter_value(
    request: Request, field: str, *, vocabulary: tuple[str, ...] | None = None
) -> str | None:
    """The validated ``filter[<field>]`` value, or None when absent (GOV4/SS1a).

    Exactly one value: a repeated param is ambiguous (which did the caller
    mean?) and is rejected, never first-one-wins (C3a: 拒絕勝於默選一邊).
    ``vocabulary`` closes the value set for enum-backed columns (the §17/DDL
    CHECK vocabularies — pass the SAME tuple a contract test pins against the
    DDL, or drift is silent); None means an open value set (e.g. entity type,
    an ontology-defined vocabulary) where only blank is meaningless.
    """
    values = request.query_params.getlist(f"filter[{field}]")
    if not values:
        return None
    if len(values) > 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"filter[{field}] accepts a single value",
            details={f"filter[{field}]": values},
        )
    value = values[0]
    if vocabulary is not None and value not in vocabulary:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"unknown {field} {value!r} — one of: {', '.join(sorted(vocabulary))}",
            details={f"filter[{field}]": value},
        )
    if vocabulary is None and not value.strip():
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"filter[{field}] must be a non-blank value",
            details={f"filter[{field}]": value},
        )
    return value
