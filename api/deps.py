"""API dependencies: the database and queue seams (BA1b/BA2e).

The FastAPI app owns one async engine for its lifetime (``lifespan``); every
request borrows a connection inside a transaction (``db_conn``) that commits on
a clean return and rolls back on any exception. Because a domain failure leaves
a handler as an ``ApiError`` propagating through the dependency's ``yield``, the
rollback also undoes any idempotency reservation — a failed write never poisons
the key. The engine is built connection-lazily, so the DB-less TestClient tests
(BA0) still start and stop the app without a live Postgres.

The arq Redis pool (``arq_redis`` — the trigger endpoints' enqueue seam) is the
same shape but must be created lazily by hand: ``create_pool`` connects eagerly,
and the app has to start without Redis for those same DB-less tests. First use
creates it (behind a lock, so concurrent first-triggers race to one pool);
lifespan disposes it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Depends, FastAPI, Request
from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from core.config import get_settings
from core.llm.factory import chat_model, embedding_model
from core.mcp.context import ProjectContext
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client


def _async_dsn() -> str:
    """The configured Postgres DSN as an asyncpg URL (never os.environ)."""
    return get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold one async engine (and, once first used, one arq pool) for the
    app's lifetime."""
    engine = create_async_engine(_async_dsn())
    app.state.engine = engine
    app.state.arq_redis = None
    app.state.arq_redis_lock = asyncio.Lock()
    app.state.neo4j = None
    app.state.qdrant = None
    app.state.embedder = None
    app.state.llm = None
    try:
        yield
    finally:
        if app.state.qdrant is not None:
            await app.state.qdrant.close()
        if app.state.neo4j is not None:
            await app.state.neo4j.close()
        if app.state.arq_redis is not None:
            await app.state.arq_redis.aclose()
        await engine.dispose()


async def db_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One transactional connection per request — commits on clean return,
    rolls back (reservation included) on any raised exception."""
    engine = request.app.state.engine
    async with engine.connect() as conn, conn.begin():
        yield conn


#: Handler signature sugar: ``conn: Conn``.
Conn = Annotated[AsyncConnection, Depends(db_conn)]


async def arq_redis(request: Request) -> ArqRedis:
    """The shared arq Redis pool, created on first use (see the module
    docstring for why it can't live eagerly in lifespan)."""
    state = request.app.state
    if state.arq_redis is None:
        async with state.arq_redis_lock:
            if state.arq_redis is None:
                state.arq_redis = await create_pool(
                    RedisSettings.from_dsn(get_settings().redis_url)
                )
    redis: ArqRedis = state.arq_redis
    return redis


def arq_redis_provider(request: Request) -> Callable[[], Awaitable[ArqRedis]]:
    """A LAZY handle on the shared arq pool — resolving the dependency does no
    I/O; the pool is opened only when the handler actually enqueues. A trigger
    must serve its §27 replay and conflict/not-found responses even with Redis
    unreachable, so the queue connection is a cost of the fresh-enqueue path
    only, never a precondition of the route."""

    def _get() -> Awaitable[ArqRedis]:
        return arq_redis(request)

    return _get


#: Handler signature sugar: ``get_redis: Queue`` — call ``await get_redis()``
#: at the enqueue point.
Queue = Annotated[Callable[[], Awaitable[ArqRedis]], Depends(arq_redis_provider)]


async def neo4j_driver(request: Request) -> AsyncDriver:
    """The shared Neo4j driver, created on first use — driver construction
    opens no connection (sessions do, per request in the handler), so this is
    zero-I/O at resolution like the arq provider. ASYNC deliberately: an async
    dependency runs on the event loop, where this await-free check-then-set is
    atomic — a sync def would run in FastAPI's threadpool, where two cold
    starts could double-construct (and leak one driver until process exit).
    Lifespan closes it if it was ever created. Tests override this dependency
    to keep the graph endpoints hermetic."""
    state = request.app.state
    if state.neo4j is None:
        state.neo4j = graph_driver()
    driver: AsyncDriver = state.neo4j
    return driver


#: Handler signature sugar: ``driver: Graph`` — open a session at the use point.
Graph = Annotated[AsyncDriver, Depends(neo4j_driver)]


async def qdrant_client(request: Request) -> AsyncQdrantClient:
    """The shared Qdrant client, created on first use — construction opens no
    connection (zero-I/O at resolution, the #53 R3 discipline), and ASYNC for
    the same event-loop check-then-set atomicity as ``neo4j_driver``.
    Lifespan closes it if it was ever created."""
    state = request.app.state
    if state.qdrant is None:
        state.qdrant = vector_client()
    client: AsyncQdrantClient = state.qdrant
    return client


#: Handler signature sugar: ``qdrant: Vectors``.
Vectors = Annotated[AsyncQdrantClient, Depends(qdrant_client)]


def project_query_context(request: Request, project: str) -> ProjectContext:
    """A per-request ProjectContext over the API's own lazily-held clients —
    the SAME bundle shape the MCP server binds per call, so the Console query
    playground and the MCP tools run one binding path (class 5). Every client
    is created at its FIRST use, never at route resolution (the #53 R3
    discipline): the qdrant client and Neo4j driver construct without I/O;
    the model factories can RAISE (no API key) and the caller maps that to a
    typed error instead of a startup failure."""
    state = request.app.state
    if state.qdrant is None:
        state.qdrant = vector_client()
    if state.neo4j is None:
        state.neo4j = graph_driver()
    if state.embedder is None:
        state.embedder = embedding_model()
    if state.llm is None:
        state.llm = chat_model()
    return ProjectContext(
        project=project,
        engine=state.engine,
        qdrant=state.qdrant,
        neo4j=state.neo4j,
        embedder=state.embedder,
        llm=state.llm,
    )


def response_meta(request: Request) -> dict[str, Any]:
    """The §15 meta kwargs for envelope.success — the request_id and elapsed
    the middleware stamped on request.state (api/app.py)."""
    return {
        "request_id": request.state.request_id,
        "elapsed_ms": int((time.monotonic() - request.state.start) * 1000),
    }
