"""Opaque cursor pagination (BA1b).

The registry pages by a keyset ``after`` tuple (BA1a); the §15 contract exposes
an OPAQUE ``meta.next_cursor``. This module is the only place the two meet: it
base64-encodes the keyset into a token the client echoes back, and decodes it —
rejecting anything malformed as a VALIDATION_ERROR rather than silently paging
from the top (which would loop or skip rows). The token is deliberately opaque:
clients must not construct or mutate it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from api.errors import ApiError, ErrorCode


def encode_cursor(values: tuple[Any, ...]) -> str:
    """Encode a keyset tuple into an opaque token. datetimes → ISO-8601,
    UUIDs → str; both round-trip through decode_*."""
    payload = [v.isoformat() if isinstance(v, datetime) else str(v) for v in values]
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _parts(token: str) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        parts = json.loads(raw)
        if not isinstance(parts, list):
            raise ValueError("cursor payload is not a list")
        return parts
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "malformed cursor", details={"cursor": token}
        ) from exc


def _convert(token: str, parts: list[Any], types: tuple[type, ...]) -> tuple[Any, ...]:
    try:
        if len(parts) != len(types):
            raise ValueError("cursor arity mismatch")
        out: list[Any] = []
        for part, typ in zip(parts, types, strict=True):
            if typ is datetime:
                parsed = datetime.fromisoformat(part)
                if parsed.tzinfo is None:
                    # every column these keysets bind against is timestamptz —
                    # a (tampered) naive value would raise in the asyncpg
                    # encoder as a 500, not the documented malformed-cursor 400
                    raise ValueError("naive datetime in cursor")
                out.append(parsed)
            elif typ is str:
                if not isinstance(part, str) or "\x00" in part:
                    # PostgreSQL text cannot carry U+0000 — a tampered NUL in
                    # the text slot would surface as a server error at bind
                    # time, not the documented malformed-cursor 400; non-str
                    # JSON shapes would be silently repr-coerced by str()
                    raise ValueError("invalid text in cursor")
                out.append(part)
            elif typ is uuid.UUID:
                if not isinstance(part, str):
                    # uuid.UUID(non-str) dies with AttributeError, which the
                    # clause below does not translate — a tampered JSON
                    # number/bool/object in the uuid slot must still be the
                    # documented malformed-cursor 400
                    raise ValueError("cursor uuid part must be a string")
                out.append(uuid.UUID(part))
            else:
                out.append(typ(part))
        return tuple(out)
    except (ValueError, TypeError) as exc:
        # a client that hands back a mangled cursor gets a clear 400, never a
        # silent reset to page one
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "malformed cursor", details={"cursor": token}
        ) from exc


def _scope_mismatch(token: str) -> ApiError:
    # the fingerprint half of a tag is an opaque hash, so the message names
    # the CLASS of mismatch rather than echoing gibberish back
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        "cursor was issued under a different listing/sort/search/filter "
        "context — restart from the first page",
        details={"cursor": token},
    )


def encode_sorted_cursor(tag: str, values: tuple[Any, ...]) -> str:
    """SS1b keysets: the cursor CARRIES the query context it was minted under
    (``sort|scope-fingerprint``, see :func:`scope_fingerprint` below). A keyset
    token only positions within ONE result set — replaying it under another
    sort would page from the wrong spot (or parse a name as a timestamp), and
    replaying it under another q/filter combination would silently skip or
    duplicate rows (R8) — so the whole context rides inside the opaque token
    and decode refuses a mismatch."""
    return encode_cursor((tag, *values))


def decode_sorted_cursor(token: str, tag: str, types: tuple[type, ...]) -> tuple[Any, ...]:
    """Decode a tagged keyset cursor, requiring its embedded context tag to
    equal the request's — a cursor issued under a different
    listing/sort/search/filter combination is a client error (400), never a
    silent re-anchor. The tag is
    checked BEFORE the value-type conversion, so a cross-context replay
    reports the actual problem instead of a misleading type-parse failure."""
    parts = _parts(token)
    if (parts[0] if parts else None) != tag:
        raise _scope_mismatch(token)
    return _convert(token, parts[1:], types)


def decode_scoped_id_cursor(token: str, tag: str) -> tuple[uuid.UUID]:
    """Default-order (id desc) cursor bound to its query scope (R8). Newly
    minted tokens carry the context tag; the legacy pre-SS1b 1-item ``(id,)``
    shape stays accepted — those in-flight cursors predate q/filter binding
    and breaking them buys nothing (the hole closes as they age out)."""
    parts = _parts(token)
    if len(parts) == 1:
        (row_id,) = _convert(token, parts, (uuid.UUID,))
        return (row_id,)
    if (parts[0] if parts else None) != tag:
        raise _scope_mismatch(token)
    (row_id,) = _convert(token, parts[1:], (uuid.UUID,))
    return (row_id,)


def scope_fingerprint(resource: str, scope_id: Any, q: str | None, applied: dict[str, str]) -> str:
    """Canonical fingerprint of the non-sort predicates a page answers (R8).

    ``resource`` names WHICH LISTING the page came from, and it is part of the
    scope for the same reason everything else here is (QA8/D15): /entities and
    /documents answer different result sets from the same build with the same
    q and filters, so without it they computed an IDENTICAL tag and each
    accepted the other's cursor — 200 with silently duplicated rows. A keyset
    anchor means nothing outside the result set that produced it, and the
    listing is part of that set's identity.

    ``scope_id`` is the OWNING AXIS of the result set, which is not always a
    build: /merge-candidates is build-scoped, /ontology-proposals is scoped by
    project (the pool has no build), and a step's items are scoped by the
    (project, build, step) triple. What it must identify is the set the keyset
    anchor was taken from — whatever names that set for this listing.

    A composite axis is passed as a TUPLE and serialized as a JSON LIST, so
    its members stay separate values and no separator-injection edge exists —
    the reason ``core/mcp/server.py``'s ``_browse_scope`` keeps its facets in
    distinct keys rather than concatenating them.
    A keyset anchor positions within ONE result set — replaying it under a
    different q/filter combination silently skips or duplicates rows — so
    this rides in the cursor tag beside the sort and decode rejects a
    mismatch, exactly as cross-sort reuse is rejected. The ACTIVE BUILD is
    part of the scope too (R9): a keyset from build A applied to build B
    would silently omit/repeat rows — the DR-001/DR-006 "never mix
    old-version data" rule, enforced here for pagination chains. Raw filter spellings
    on purpose: "3" vs "3.0" fingerprint differently, which over-rejects a
    cosmetic respelling but never under-rejects a real predicate change."""
    # A composite axis stays a LIST through the JSON, so its members are
    # separate values rather than one joined string — ("a/b", "c") and
    # ("a", "b/c") must not fingerprint alike.
    axis = [str(part) for part in scope_id] if isinstance(scope_id, tuple) else str(scope_id)
    scope = json.dumps(
        {"resource": resource, "scope": axis, "q": q or "", "filters": applied},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(scope.encode()).hexdigest()[:12]
