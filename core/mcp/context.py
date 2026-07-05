"""Per-project runtime context: long-lived engines, per-call bound stores (C8).

The MCP server holds ONE set of engines/clients/models for its project's
lifetime; every tool call binds a FRESH set of build-scoped stores off them
(DR-001: the active build is re-resolved per call, so an activation between
calls is picked up; §27.1's "read once per request" — the bound repos ARE that
cache for the duration of one call).

Mint order is load-bearing (the C6e wiring lesson): the SQL reader's
loaned-clean contract (C6b) refuses a connection another factory's lookup has
already begun a transaction on, so it is minted FIRST on the fresh
connection. The whole call runs read-only single-flight on that connection;
hybrid's sql phase may roll back auto-begun READ transactions of sibling
modes — harmless here by construction (nothing writes on this connection).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from core.query.hybrid import HybridDeps
from core.stores.graph import BuildScopedGraphRepo
from core.stores.repo import BuildScopedRepo
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.vectors import BuildScopedVectorRepo


@dataclass(frozen=True)
class ProjectContext:
    """One project's long-lived runtime: engines + models + its name.

    Built once at server startup (engines are pooled/reused), closed at
    shutdown; never bound to a build — binding happens per call."""

    project: str
    engine: AsyncEngine
    qdrant: AsyncQdrantClient
    neo4j: AsyncDriver
    embedder: BaseEmbedding
    llm: LLM

    @asynccontextmanager
    async def bound(self) -> AsyncIterator[HybridDeps]:
        """Bind every store to the CURRENT active build for one tool call.

        Yields the full :class:`~core.query.hybrid.HybridDeps` (single-mode
        tools use the slice they need — one binding path keeps the scope
        agreement DR-006 demands, and hybrid re-verifies it anyway). The
        connection and graph session live exactly as long as the call."""
        async with self.engine.connect() as conn, self.neo4j.session() as session:
            # sql reader FIRST — its loaned-clean contract refuses a
            # connection with an open transaction, and the other factories'
            # lookups auto-begin one (C6b/C6e)
            sql_reader = await BuildScopedSqlReader.for_active_build(conn, self.project)
            repo = await BuildScopedRepo.for_active_build(conn, self.project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, self.qdrant, self.project)
            graph = await BuildScopedGraphRepo.for_active_build(conn, session, self.project)
            yield HybridDeps(
                repo=repo,
                vectors=vectors,
                embedder=self.embedder,
                sql_reader=sql_reader,
                graph=graph,
                llm=self.llm,
            )

    async def aclose(self) -> None:
        """Release the long-lived engines (server shutdown)."""
        await self.qdrant.close()
        await self.neo4j.close()
        await self.engine.dispose()
