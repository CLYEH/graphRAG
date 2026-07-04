"""Build-scoped projection repository over Qdrant (DESIGN §4/§27.1, DR-006, C1d).

Qdrant is a *derived projection* of Postgres: one collection per project
(§4), and inside it every point's payload carries ``{project, build_id,
canonical_id, type, text, chunk_id|entity_id}``. Two builds' points coexist
in the same collection; DR-006 demands that mixing them is structurally
impossible. This module is that structure for Qdrant — the third sibling of
the Postgres (`core.stores.repo`) and Neo4j (`core.stores.graph`) repos:

- **No API accepts a filter.** Qdrant has no query text at all (the client
  API is structured data), so the residual injection surface is the filter
  object: a caller-supplied ``models.Filter`` could ``should``/``min_should``
  its way around a naive merge. C1d closes that surface the same way C1c
  closed Cypher — it does not exist. Callers pass typed values
  (``point_type``, ``limit``); the only filter ever sent is the one this
  module builds, with the scope conditions in a top-level ``must`` (ANDed).
  C6a extends this additively when real retrieval needs richer narrowing.
- ``BuildScopedVectorRepo`` — the READ capability: bound to the active build
  resolved from **Postgres** (DR-001), it injects the scope filter into every
  search and count. The client is name-mangled private, ``__slots__``,
  construction-token fenced, read/write split — the same capability fence as
  the sibling repos.
- ``BuildScopedVectorProjector`` — the WRITE capability (C5's consumer),
  minted only by the validating
  :meth:`~BuildScopedVectorProjector.for_building_build` factory. Writes
  inject the scope INTO each point's payload (merged last — a caller-supplied
  project/build_id cannot re-point the write), and every write first
  revalidates the build status ``FOR SHARE`` on the projector's Postgres
  connection: activation is a single Postgres transaction (§14) that must
  lock that row, so in-flight upsert transactions and activation are
  mutually exclusive — the same cross-store TOCTOU anchor as the Neo4j
  projector, proven live in the integration tests.

Point ids are the build-scoped Postgres row ids (chunks/entities mint fresh
uuids per build), so re-projecting build B can never overwrite build A's
points — id uniqueness across builds is inherited from the source of truth.
Upserts use ``wait=True``: the §5 pipeline needs read-after-write (skip /
rerun decisions), and correctness beats throughput until profiling says
otherwise. Deletion/pruning is C9's; richer search is C6a's.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import get_settings
from core.stores import tables
from core.stores.repo import BuildNotWritableError, active_build_id


def vector_client() -> AsyncQdrantClient:
    """Qdrant client from central settings (core.config — never os.environ)."""
    return AsyncQdrantClient(url=get_settings().qdrant_url)


def collection_for(project: str) -> str:
    """§4: one collection per project, as a DERIVED safe name.

    The project contract (P0) only requires a non-empty string, but Qdrant
    collection names are URL-path identifiers with their own character/length
    restrictions — mapping the raw key would make some contract-valid
    projects unindexable. So the name is derived deterministically: a
    sanitized prefix keeps it human-readable, and the content-hash suffix
    keeps two projects distinct even when sanitization (or truncation) would
    collide them (e.g. ``ab/c`` vs ``ab_c``).

    The digest is 32 hex chars (128 bits): §4's one-collection-per-project is
    an INVARIANT, so the suffix must hold against adversarial prefix-sharing
    names, not just accidents — a 10-hex suffix (40 bits) was demonstrated
    brute-forceable in review. 128 bits puts the birthday bound at ~2^64.
    Total length 73 ≤ Qdrant's 255 (verified live).
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", project)[:32]
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:32]
    return f"project_{safe}_{digest}"


#: Module-private construction token — factories are the only sanctioned
#: bindings, same fence as the Postgres and Neo4j repos.
_CONSTRUCTION_TOKEN = object()


class BuildScopedVectorRepo:
    """Read-only Qdrant access bound to one ``(project, build_id)`` (DR-006).

    Construct via :meth:`for_active_build`. Every search/count carries the
    scope filter this module builds; no method accepts a caller filter, so
    nothing can widen a read across builds.
    """

    __slots__ = ("__client", "__project", "__build_id")

    def __init__(
        self,
        client: AsyncQdrantClient,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError(
                "construct via BuildScopedVectorRepo.for_active_build (reads) or "
                "BuildScopedVectorProjector.for_building_build (pipeline projection) — "
                "direct construction would skip the scope validation those factories do"
            )
        self.__client = client
        self.__project = project
        self.__build_id = build_id

    @property
    def project(self) -> str:
        return self.__project

    @property
    def build_id(self) -> uuid.UUID:
        return self.__build_id

    @property
    def _collection(self) -> str:
        return collection_for(self.__project)

    @classmethod
    async def for_active_build(
        cls, pg_conn: AsyncConnection, client: AsyncQdrantClient, project: str
    ) -> BuildScopedVectorRepo:
        """Bind to the project's active build — resolved from POSTGRES (DR-001).

        Pinned to return the read-only type (not ``cls``) so a subclass can
        never mint an active-bound projector.
        """
        build = await active_build_id(pg_conn, project)
        return BuildScopedVectorRepo(client, project, build, _token=_CONSTRUCTION_TOKEN)

    # -- scope plumbing --------------------------------------------------------

    def _scope_conditions(self) -> list[models.Condition]:
        # payloads store build_id in its string form (uuid is not a JSON type)
        return [
            models.FieldCondition(key="project", match=models.MatchValue(value=self.__project)),
            models.FieldCondition(
                key="build_id", match=models.MatchValue(value=str(self.__build_id))
            ),
        ]

    def _scope_filter(self, point_type: str | None = None) -> models.Filter:
        """The ONLY filter this layer ever sends: scope in a top-level `must`
        (ANDed — additional typed conditions can only narrow, never escape)."""
        must = self._scope_conditions()
        if point_type is not None:
            must.append(
                models.FieldCondition(key="type", match=models.MatchValue(value=point_type))
            )
        return models.Filter(must=must)

    @property
    def _client(self) -> AsyncQdrantClient:
        # the internal seam (single-underscore, like repo._execute/graph._run):
        # the mangled attribute is unreachable from subclasses, internal
        # methods reach Qdrant only through here, and reaching it from outside
        # is a deliberate, review-visible bypass — never a public convenience
        return self.__client

    # -- the public, executing surface ----------------------------------------

    async def search(
        self, vector: list[float], limit: int, point_type: str | None = None
    ) -> list[models.ScoredPoint]:
        """Scoped kNN: nearest points of the bound build (optionally one type)."""
        response = await self._client.query_points(
            self._collection,
            query=vector,
            query_filter=self._scope_filter(point_type),
            limit=limit,
        )
        return response.points

    async def point_count(self, point_type: str | None = None) -> int:
        """Scoped exact count — §19 projection-drift reconciliation (PG vs Qdrant)."""
        result = await self._client.count(
            self._collection, count_filter=self._scope_filter(point_type), exact=True
        )
        return result.count


class BuildScopedVectorProjector(BuildScopedVectorRepo):
    """The pipeline projection capability (C5 writes; §27.1 building-only).

    Exists ONLY via :meth:`for_building_build`. Bind-time validation is
    ergonomics; the invariant is per write: :meth:`_assert_building` re-reads
    the build status ``FOR SHARE`` on the held Postgres connection before
    every upsert, making activation and in-flight projection transactions
    mutually exclusive at the Postgres row lock — the one place both meet.
    """

    __slots__ = ("__pg_conn",)

    def __init__(
        self,
        pg_conn: AsyncConnection,
        client: AsyncQdrantClient,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        super().__init__(client, project, build_id, _token=_token)
        self.__pg_conn = pg_conn

    @classmethod
    async def for_building_build(
        cls,
        pg_conn: AsyncConnection,
        client: AsyncQdrantClient,
        project: str,
        build_id: uuid.UUID,
    ) -> BuildScopedVectorProjector:
        """Bind a projector to a VALIDATED ``building`` build (§27.1)."""
        status: str | None = (
            await pg_conn.execute(
                sa.select(tables.builds.c.status).where(
                    tables.builds.c.id == build_id,
                    tables.builds.c.project == project,
                )
            )
        ).scalar_one_or_none()
        if status != "building":
            raise BuildNotWritableError(project, build_id, status)
        return BuildScopedVectorProjector(
            pg_conn, client, project, build_id, _token=_CONSTRUCTION_TOKEN
        )

    async def _assert_building(self) -> None:
        # FOR SHARE inside the caller's open Postgres transaction: the lock
        # outlives this check and the Qdrant write that follows, and conflicts
        # with the activation UPDATE's row lock (see module docstring)
        status: str | None = (
            await self.__pg_conn.execute(
                sa.select(tables.builds.c.status)
                .where(
                    tables.builds.c.id == self.build_id,
                    tables.builds.c.project == self.project,
                )
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if status != "building":
            raise BuildNotWritableError(self.project, self.build_id, status)

    async def ensure_collection(self, vector_size: int) -> None:
        """Idempotently create the project's collection (§4: one per project).

        Revalidated like every other projector write: creation is not
        build-tagged data, but it FREEZES the shared collection's vector
        schema — a stale projector (bound to a build that already activated
        or failed) could otherwise create it with the wrong size and break
        the next build's indexing. The write license expiring means ALL its
        side effects stop, not just the payload-tagged ones.

        Cosine distance — the natural metric for the normalized OpenAI
        embeddings C5 stores (§3). Concurrent first-creation races surface as
        the client's conflict error; C5's orchestration serializes step
        startup, so that path stays fail-loud rather than silently retried.
        """
        await self._assert_building()
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                self._collection,
                vectors_config=models.VectorParams(
                    size=vector_size, distance=models.Distance.COSINE
                ),
            )

    async def upsert_point(
        self,
        point_id: uuid.UUID,
        vector: list[float],
        *,
        canonical_id: str,
        point_type: str,
        text: str,
        chunk_id: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        """Upsert one point with the §4 payload, scope injected.

        ``point_id`` is the build-scoped Postgres row id (chunks/entities
        mint fresh uuids per build), so builds cannot overwrite each other's
        points and §5 retries overwrite only their own. The payload is built
        entirely from these typed fields — there is no caller-supplied
        payload dict, so no way to hand in a foreign project/build_id at all;
        the scope keys are set by this module from the binding.
        """
        await self._assert_building()
        payload: dict[str, Any] = {
            "canonical_id": canonical_id,
            "type": point_type,
            "text": text,
            "chunk_id": chunk_id,
            "entity_id": entity_id,
            "project": self.project,
            "build_id": str(self.build_id),
        }
        await self._client.upsert(
            self._collection,
            points=[models.PointStruct(id=str(point_id), vector=vector, payload=payload)],
            wait=True,
        )
