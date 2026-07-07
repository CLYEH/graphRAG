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


def _decode(token: str, types: tuple[type, ...]) -> tuple[Any, ...]:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        parts = json.loads(raw)
        if not isinstance(parts, list) or len(parts) != len(types):
            raise ValueError("cursor arity mismatch")
        out: list[Any] = []
        for part, typ in zip(parts, types, strict=True):
            if typ is datetime:
                out.append(datetime.fromisoformat(part))
            elif typ is uuid.UUID:
                out.append(uuid.UUID(part))
            else:
                out.append(typ(part))
        return tuple(out)
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        # a client that hands back a mangled cursor gets a clear 400, never a
        # silent reset to page one
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "malformed cursor", details={"cursor": token}
        ) from exc


def decode_project_cursor(token: str) -> tuple[datetime, str]:
    created_at, name = _decode(token, (datetime, str))
    return created_at, name


def decode_source_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    added_at, sid = _decode(token, (datetime, uuid.UUID))
    return added_at, sid
