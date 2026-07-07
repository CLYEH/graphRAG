"""§27 Idempotency-Key handling (BA1b) — reserve-first, PK-serialized.

An HTTP concern (it hashes method+path+body and replays an HTTP status+body), so
it lives in ``api/`` and raises ApiError. The algorithm, all inside the request's
transaction:

1. purge this key if expired (a reuse past the TTL is a fresh request);
2. RESERVE the key with an ``INSERT ... ON CONFLICT DO NOTHING`` — the row's
   PK both stores the one response and serializes concurrent same-key requests
   (a second insert of a still-live key blocks on the first's txn, then finds
   the row);
3. if we did not win the reservation, the key already exists: a DIFFERENT
   request_hash is a 409 IDEMPOTENCY_CONFLICT, a matching one replays the
   stored status+body verbatim;
4. if we won, run the handler, store its status+body, return them.

A handler that fails raises out through ``db_conn``'s transaction, rolling the
reservation back — so a failed write never leaves a poisoned key. Clock is the
database's throughout (created_at/expires_at via ``now()``), never the app's.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import sqlalchemy as sa
from fastapi.encoders import jsonable_encoder
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from api.errors import ApiError, ErrorCode
from core.config import get_settings
from core.stores import tables

#: A handler run: returns (http_status, envelope_body).
Producer = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


def request_hash(method: str, path: str, body: bytes) -> str:
    """Stable hash of the request identity. JSON bodies are canonicalized
    (sorted keys, no whitespace) so a cosmetic reformat is not a false
    conflict; a non-JSON body hashes raw."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, TypeError):
        canonical = body
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(b"\n")
    h.update(path.encode())
    h.update(b"\n")
    h.update(canonical)
    return h.hexdigest()


async def run_idempotent(
    conn: AsyncConnection,
    *,
    key: str,
    project: str,
    endpoint: str,
    req_hash: str,
    produce: Producer,
) -> tuple[int, dict[str, Any]]:
    """Replay, reject, or run-and-store — see the module docstring."""
    idem = tables.idempotency_keys
    ttl = int(get_settings().idempotency_ttl_hours)

    # 1. expired reuse → drop the stale row so the reserve below runs fresh
    await conn.execute(idem.delete().where(idem.c.key == key, idem.c.expires_at <= sa.func.now()))

    # 2. reserve (DB clock: created_at defaults to now(), expires_at = now()+TTL)
    reserved = (
        await conn.execute(
            pg_insert(idem)
            .values(
                key=key,
                project=project,
                endpoint=endpoint,
                request_hash=req_hash,
                response=None,
                status=None,
                expires_at=sa.text(f"now() + make_interval(hours => {ttl})"),
            )
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(idem.c.key)
        )
    ).scalar_one_or_none()

    # 3. lost the race → the key is already live: conflict or replay
    if reserved is None:
        row = (
            await conn.execute(
                sa.select(idem.c.request_hash, idem.c.response, idem.c.status).where(
                    idem.c.key == key
                )
            )
        ).one()
        if row.request_hash != req_hash:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key reused with a different request",
                details={"key": key},
            )
        return int(row.status), row.response

    # 4. won the reservation → run, store the encoded result, return it
    status, body = await produce()
    encoded: dict[str, Any] = jsonable_encoder(body)
    await conn.execute(
        idem.update().where(idem.c.key == key).values(response=encoded, status=status)
    )
    return status, encoded
