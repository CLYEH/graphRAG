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


def _decode(token: str, types: tuple[type, ...]) -> tuple[Any, ...]:
    return _convert(token, _parts(token), types)


def decode_project_cursor(token: str) -> tuple[datetime, str]:
    created_at, name = _decode(token, (datetime, str))
    return created_at, name


def decode_source_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    added_at, sid = _decode(token, (datetime, uuid.UUID))
    return added_at, sid


def decode_step_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """RB1 build-step drill-down pages NEWEST RUN FIRST — the keyset is the run's
    (coalesced) started_at plus the step id tie-break, so the cursor is a
    (datetime, uuid) pair like sources' (BA3/RB1)."""
    run_started_at, step_id = _decode(token, (datetime, uuid.UUID))
    return run_started_at, step_id


def decode_id_cursor(token: str) -> tuple[uuid.UUID]:
    """Documents/entities/relations page by (id desc) — documents carry no
    created_at and entities/relations only a NULLABLE one, so id is the stable
    unique keyset shared by all three; recency ordering can land additively
    with the Sort param later (BA3a/BA3b)."""
    (row_id,) = _decode(token, (uuid.UUID,))
    return (row_id,)


def decode_chunk_cursor(token: str) -> tuple[uuid.UUID, int]:
    """Chunks page by (document_id asc, ordinal asc) — the reading order, and
    UNIQUE(document_id, ordinal) makes it a total keyset (BA3a)."""
    document_id, ordinal = _decode(token, (uuid.UUID, int))
    return document_id, ordinal


def _scope_mismatch(token: str) -> ApiError:
    # the fingerprint half of a tag is an opaque hash, so the message names
    # the CLASS of mismatch rather than echoing gibberish back
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        "cursor was issued under a different sort/search/filter context — "
        "restart from the first page",
        details={"cursor": token},
    )


def encode_sorted_cursor(tag: str, values: tuple[Any, ...]) -> str:
    """SS1b keysets: the cursor CARRIES the query context it was minted under
    (``sort|scope-fingerprint``, see inspect._scope_fingerprint). A keyset
    token only positions within ONE result set — replaying it under another
    sort would page from the wrong spot (or parse a name as a timestamp), and
    replaying it under another q/filter combination would silently skip or
    duplicate rows (R8) — so the whole context rides inside the opaque token
    and decode refuses a mismatch."""
    return encode_cursor((tag, *values))


def decode_sorted_cursor(token: str, tag: str, types: tuple[type, ...]) -> tuple[Any, ...]:
    """Decode a tagged keyset cursor, requiring its embedded context tag to
    equal the request's — a cursor issued under a different sort/search/filter
    combination is a client error (400), never a silent re-anchor. The tag is
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
