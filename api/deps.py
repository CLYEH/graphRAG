"""API dependencies: the database seam (BA1b).

The FastAPI app owns one async engine for its lifetime (``lifespan``); every
request borrows a connection inside a transaction (``db_conn``) that commits on
a clean return and rolls back on any exception. Because a domain failure leaves
a handler as an ``ApiError`` propagating through the dependency's ``yield``, the
rollback also undoes any idempotency reservation — a failed write never poisons
the key. The engine is built connection-lazily, so the DB-less TestClient tests
(BA0) still start and stop the app without a live Postgres.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from core.config import get_settings


def _async_dsn() -> str:
    """The configured Postgres DSN as an asyncpg URL (never os.environ)."""
    return get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold one async engine for the app's lifetime."""
    engine = create_async_engine(_async_dsn())
    app.state.engine = engine
    try:
        yield
    finally:
        await engine.dispose()


async def db_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One transactional connection per request — commits on clean return,
    rolls back (reservation included) on any raised exception."""
    engine = request.app.state.engine
    async with engine.connect() as conn, conn.begin():
        yield conn


#: Handler signature sugar: ``conn: Conn``.
Conn = Annotated[AsyncConnection, Depends(db_conn)]


def response_meta(request: Request) -> dict[str, Any]:
    """The §15 meta kwargs for envelope.success — the request_id and elapsed
    the middleware stamped on request.state (api/app.py)."""
    return {
        "request_id": request.state.request_id,
        "elapsed_ms": int((time.monotonic() - request.state.start) * 1000),
    }
