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
            elif typ is uuid.UUID:
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


def encode_sorted_cursor(sort: str, values: tuple[Any, ...]) -> str:
    """SS1b sorted keysets: the cursor CARRIES the sort it was minted under.
    A keyset token only means something relative to its ORDER BY — replaying
    a canonical_name cursor into a created_at-sorted request would silently
    page from the wrong spot (or worse, parse a name as a timestamp), so the
    sort spelling rides inside the opaque token and decode refuses a
    mismatch. The DEFAULT (id desc) order keeps the legacy untagged shape:
    in-flight pre-SS1b cursors stay valid, and every cross-shape replay
    fails the arity check."""
    return encode_cursor((sort, *values))


def decode_sorted_cursor(token: str, sort: str, types: tuple[type, ...]) -> tuple[Any, ...]:
    """Decode a sorted-keyset cursor, requiring its embedded sort to equal the
    request's ``sort`` — a cursor issued under a different order is a client
    error (400), never a silent re-anchor. The tag is checked BEFORE the
    value-type conversion, so a cross-sort replay reports the actual problem
    ("issued under sort=X") instead of a misleading type-parse failure."""
    parts = _parts(token)
    tag = parts[0] if parts else None
    if tag != sort:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"cursor was issued under sort={tag!r} and cannot page a "
            f"sort={sort!r} request — restart from the first page",
            details={"cursor": token, "sort": sort},
        )
    return _convert(token, parts[1:], types)
