"""Build-scoped projection repository over Neo4j (DESIGN §4/§27.1, DR-004/DR-006, C1c).

Neo4j is a *derived projection* of Postgres (DR-004: one single database,
Community-compatible — no multi-db), so version isolation is carried by data:
every ``(:Entity)`` node holds ``{canonical_id, build_id, project, type,
status, ...}`` and every ``[:REL]`` relationship holds ``{build_id, type}``
(§4). Two builds' graphs coexist in the same store; DR-006 demands that
mixing them is structurally impossible, not a query-discipline convention.
This module is that structure for Neo4j:

- **No API accepts Cypher.** The whole read/write surface is fixed
  ``LiteralString`` templates defined in this module; callers pass parameter
  VALUES only, bound by the driver. There is nothing to guard against
  splicing because there is no splice point — the lesson of C1b's predicate
  guard (every sibling raw-SQL API had to be enumerated) applied one level
  earlier. §4 helps here: relationships are a fixed ``:REL`` type with the
  semantic type as a *property* (properties parameterize; relationship types
  and labels in Cypher cannot), so even the projection needs no dynamic
  query text.
- ``BuildScopedGraphRepo`` — the READ capability: binds ``(project,
  build_id)`` once (the active build resolved from **Postgres**, DR-001 —
  Neo4j has no say in what is active) and every template filters both node
  endpoints and relationships by the bound scope. The session is
  name-mangled private and execution happens inside the repo, mirroring the
  Postgres repo's fence.
- ``BuildScopedGraphProjector`` — the WRITE capability (C5's consumer), a
  separate type produced only by the validating
  :meth:`~BuildScopedGraphProjector.for_building_build` factory. §27.1's
  "writes target a *building* build" cannot be made atomic with the write in
  a single statement here (the status lives in Postgres, the write in
  Neo4j), so the invariant is anchored on the Postgres row lock instead:
  every projection write first re-reads the build's status ``FOR SHARE`` on
  the projector's Postgres connection. Activation is a single Postgres
  transaction (§14) whose ``UPDATE builds`` needs that row's lock, so it
  blocks until the projecting transaction ends — the Neo4j write happens
  strictly before or strictly after activation, never astride it. (A plain
  recheck would race an uncommitted activation: MVCC readers don't block
  writers.)

The surface is deliberately minimal: entity/relation projection for C5,
scoped fetch + the two counts §19's projection-drift reconciliation needs,
and C6c's retrieval templates (neighbors/path/subgraph — §21/§27.6's
parameterized default graph path).

One deliberate seam in the no-dynamic-text rule: Cypher cannot parameterize a
variable-length bound (``*1..$hops`` is a syntax error — verified live), so
the two traversal templates carry a ``__HOPS__`` placeholder substituted by
:meth:`BuildScopedGraphRepo._hop_template` with a VALIDATED ``int`` — the
same contained exception as the SQL reader's ``int(timeout_ms)`` embed: an
integer literal cannot smuggle query text.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final, LiteralString

import sqlalchemy as sa
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import get_settings
from core.stores import tables
from core.stores.repo import BuildNotWritableError, active_build_id

#: Every template filters BOTH ``:Entity`` endpoints by ``{build_id, project}``
#: and every ``:REL`` by ``{build_id}`` — the §4 projection rule. They are
#: ``Final`` literals so the driver's ``LiteralString`` signature (and mypy
#: strict) rejects any dynamically-built query at the type level.

_FETCH_ENTITIES: Final = """\
MATCH (n:Entity {build_id: $build_id, project: $project})
WHERE $entity_type IS NULL OR n.type = $entity_type
RETURN n{.*} AS entity
ORDER BY n.canonical_id
"""

_ENTITY_COUNT: Final = """\
MATCH (n:Entity {build_id: $build_id, project: $project})
RETURN count(n) AS total
"""

_RELATION_COUNT: Final = """\
MATCH (:Entity {build_id: $build_id, project: $project})
      -[r:REL {build_id: $build_id}]->
      (:Entity {build_id: $build_id, project: $project})
RETURN count(r) AS total
"""

# -- C6c retrieval templates (§27.6: the parameterized default graph path) ----
#
# The variable-length templates re-filter the WHOLE path with
# `all(n IN nodes(p) …)`: the endpoint property maps do NOT constrain the
# intermediate nodes a multi-hop path passes through, so without it a
# traversal could tunnel THROUGH a rejected/merged node to reach an active
# one — surfacing a connection the active graph does not have. (Relationships
# are already per-hop scoped: a property map inside a variable-length pattern
# applies to every hop — verified live.)

_NEIGHBORS: Final = """\
MATCH p = (seed:Entity {canonical_id: $seed, status: 'active',
                        build_id: $build_id, project: $project})
          -[:REL*1..__HOPS__ {build_id: $build_id}]-
          (m:Entity {build_id: $build_id, project: $project, status: 'active'})
WHERE m.canonical_id <> $seed
  AND all(n IN nodes(p) WHERE n.project = $project
          AND n.build_id = $build_id AND n.status = 'active')
WITH m, min(length(p)) AS distance
RETURN m{.*} AS entity, distance
ORDER BY distance, m.canonical_id
LIMIT $limit
"""

_SHORTEST_PATH: Final = """\
MATCH (src:Entity {canonical_id: $src, status: 'active',
                   build_id: $build_id, project: $project}),
      (dst:Entity {canonical_id: $dst, status: 'active',
                   build_id: $build_id, project: $project}),
      p = shortestPath((src)-[:REL*..__HOPS__]-(dst))
WHERE all(rel IN relationships(p) WHERE rel.build_id = $build_id)
  AND all(n IN nodes(p) WHERE n.project = $project
          AND n.build_id = $build_id AND n.status = 'active')
RETURN [n IN nodes(p) | n{.*}] AS nodes,
       [rel IN relationships(p) | {type: rel.type,
                                   src: startNode(rel).canonical_id,
                                   dst: endNode(rel).canonical_id}] AS rels
LIMIT 1
"""

_EDGES_AMONG: Final = """\
MATCH (x:Entity {build_id: $build_id, project: $project, status: 'active'})
      -[r:REL {build_id: $build_id}]->
      (y:Entity {build_id: $build_id, project: $project, status: 'active'})
WHERE x.canonical_id IN $ids AND y.canonical_id IN $ids
RETURN x.canonical_id AS src, y.canonical_id AS dst, r.type AS type
ORDER BY src, dst, type
"""

_PROJECT_ENTITY: Final = """\
MERGE (n:Entity {canonical_id: $canonical_id, build_id: $build_id, project: $project})
SET n.type = $entity_type, n.status = $status, n.name = $name
"""

_PROJECT_RELATION: Final = """\
MATCH (src:Entity {canonical_id: $src, build_id: $build_id, project: $project})
MATCH (dst:Entity {canonical_id: $dst, build_id: $build_id, project: $project})
MERGE (src)-[r:REL {build_id: $build_id, type: $rel_type}]->(dst)
RETURN count(r) AS linked
"""


def graph_driver() -> AsyncDriver:
    """Neo4j driver from central settings (core.config — never os.environ)."""
    settings = get_settings()
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )


class RelationEndpointsNotProjectedError(LookupError):
    """A relation projection named endpoints absent from the bound build.

    C5 projects entities before relations; an endpoint missing here means
    that ordering (or the entity projection itself) broke. A silent MERGE
    no-op would hide exactly that bug, so the projector refuses loudly.
    """

    def __init__(self, project: str, build_id: uuid.UUID, src: str, dst: str) -> None:
        super().__init__(
            f"cannot project relation {src!r}->{dst!r}: one or both entities are "
            f"not projected in build {build_id} of project {project!r}"
        )
        self.project = project
        self.build_id = build_id
        self.src = src
        self.dst = dst


#: Module-private construction token — the factories below are the only
#: sanctioned bindings (active for reads, VALIDATED building for writes),
#: same fence as the Postgres repo.
_CONSTRUCTION_TOKEN = object()


class BuildScopedGraphRepo:
    """Read-only Neo4j access bound to one ``(project, build_id)`` (DR-006).

    Construct via :meth:`for_active_build`. The scope is read-only after
    construction, execution happens inside the repo (the session is
    name-mangled private), and no method accepts query text — reads are the
    fixed templates above with the scope injected as parameters.
    """

    __slots__ = ("__session", "__project", "__build_id")

    def __init__(
        self,
        session: AsyncSession,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError(
                "construct via BuildScopedGraphRepo.for_active_build (reads) or "
                "BuildScopedGraphProjector.for_building_build (pipeline projection) — "
                "direct construction would skip the scope validation those factories do"
            )
        self.__session = session
        self.__project = project
        self.__build_id = build_id

    @property
    def project(self) -> str:
        return self.__project

    @property
    def build_id(self) -> uuid.UUID:
        return self.__build_id

    @classmethod
    async def for_active_build(
        cls, pg_conn: AsyncConnection, session: AsyncSession, project: str
    ) -> BuildScopedGraphRepo:
        """Bind to the project's active build — resolved from POSTGRES (DR-001).

        Neo4j holds several builds' projections at once (DR-004); which one is
        live is solely Postgres's decision. Pinned to return the read-only
        type (not ``cls``) so a subclass cannot mint an active-bound writer.
        """
        build = await active_build_id(pg_conn, project)
        return BuildScopedGraphRepo(session, project, build, _token=_CONSTRUCTION_TOKEN)

    # -- scope plumbing --------------------------------------------------------

    def _scope_params(self) -> dict[str, str]:
        # Neo4j has no native UUID type — build_id is projected as its string
        # form, so the filter parameter must be the same representation
        return {"build_id": str(self.__build_id), "project": self.__project}

    async def _run(self, template: LiteralString, params: dict[str, Any]) -> list[dict[str, Any]]:
        # the sole execution seam; scope params are merged LAST so nothing a
        # caller passes can override the binding
        result = await self.__session.run(template, {**params, **self._scope_params()})
        return await result.data()

    # -- the public, executing surface ----------------------------------------

    async def fetch_entities(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        """Read the bound build's entity nodes, optionally narrowed by type."""
        rows = await self._run(_FETCH_ENTITIES, {"entity_type": entity_type})
        return [row["entity"] for row in rows]

    async def entity_count(self) -> int:
        """Scoped node count — §19 projection-drift reconciliation (PG vs Neo4j)."""
        rows = await self._run(_ENTITY_COUNT, {})
        total: int = rows[0]["total"]
        return total

    async def relation_count(self) -> int:
        """Scoped relationship count — §19 projection-drift reconciliation."""
        rows = await self._run(_RELATION_COUNT, {})
        total: int = rows[0]["total"]
        return total

    # -- C6c retrieval templates (§27.6 default graph path) --------------------

    @staticmethod
    def _hop_template(template: LiteralString, hops: int) -> str:
        """Substitute the ``__HOPS__`` placeholder with a VALIDATED int.

        The one sanctioned exception to the fixed-LiteralString rule (see the
        module docstring): a variable-length bound cannot be a driver
        parameter, and an ``int``'s decimal rendering cannot carry query text.
        ``type() is int`` (not isinstance) also refuses ``bool`` — ``True``
        would render as the string ``True`` mid-pattern."""
        if type(hops) is not int or hops < 1:
            raise ValueError(f"hops must be a positive int, got {hops!r}")
        return template.replace("__HOPS__", str(hops))

    async def _run_read(
        self, query: str, params: dict[str, Any], timeout_ms: int
    ) -> list[dict[str, Any]]:
        """Run one retrieval template under the policy deadline (§21/§22).

        ``timeout`` is enforced server-side by Neo4j (the transaction is
        killed at the deadline and the driver raises), so a runaway traversal
        cannot hold the session past the policy budget. The ``query`` str is
        either a module template verbatim or one that went through
        :meth:`_hop_template` — this private method takes no caller text, so
        it does not open a caller-facing splice point."""
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError(f"timeout_ms must be a positive int, got {timeout_ms!r}")
        result = await self.__session.run(
            Query(query, timeout=timeout_ms / 1000.0),
            {**params, **self._scope_params()},
        )
        return await result.data()

    async def neighbors(
        self, seed: str, *, hops: int, limit: int, timeout_ms: int
    ) -> list[dict[str, Any]]:
        """Active entities within ``hops`` of ``seed``, nearest first.

        Each row is ``{entity: {…node properties…}, distance: int}``. The
        template excludes the seed itself, scopes every hop's relationship and
        EVERY node on the path (incl. intermediates) to the bound build's
        active entities, and caps the result at ``limit`` (the §21 row cap —
        parameterizable, unlike the hop bound)."""
        if type(limit) is not int or limit < 1:
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        return await self._run_read(
            self._hop_template(_NEIGHBORS, hops), {"seed": seed, "limit": limit}, timeout_ms
        )

    async def shortest_path(
        self, src: str, dst: str, *, max_hops: int, timeout_ms: int
    ) -> dict[str, Any] | None:
        """One shortest active path ``src`` → ``dst`` within ``max_hops``, or
        ``None``. Returns ``{nodes: [{…}, …], rels: [{type, src, dst}, …]}`` —
        the rels carry endpoint canonical_ids so the caller can map every edge
        back to its SoR relation row (§27.2: a path cites every edge)."""
        rows = await self._run_read(
            self._hop_template(_SHORTEST_PATH, max_hops), {"src": src, "dst": dst}, timeout_ms
        )
        return rows[0] if rows else None

    async def edges_among(
        self, canonical_ids: Sequence[str], *, timeout_ms: int
    ) -> list[dict[str, Any]]:
        """All active edges whose BOTH endpoints are in ``canonical_ids`` —
        the subgraph template's edge set (nodes come from :meth:`neighbors`)."""
        if not canonical_ids:
            return []
        return await self._run_read(_EDGES_AMONG, {"ids": list(canonical_ids)}, timeout_ms)


class BuildScopedGraphProjector(BuildScopedGraphRepo):
    """The pipeline projection capability (C5 writes; §27.1 building-only).

    Exists ONLY via :meth:`for_building_build`. The bind-time status check is
    ergonomics (fail early, typed); the invariant is per write:
    :meth:`_assert_building` re-reads the build's status ``FOR SHARE`` on the
    held Postgres connection before every Neo4j write, so activation (§14: a
    single Postgres transaction that must lock that row) is mutually
    exclusive with in-flight projection transactions — cross-store TOCTOU
    closed at the Postgres row lock, the one place both sides meet.
    """

    __slots__ = ("__pg_conn",)

    def __init__(
        self,
        pg_conn: AsyncConnection,
        session: AsyncSession,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        super().__init__(session, project, build_id, _token=_token)
        self.__pg_conn = pg_conn

    @classmethod
    async def for_building_build(
        cls,
        pg_conn: AsyncConnection,
        session: AsyncSession,
        project: str,
        build_id: uuid.UUID,
    ) -> BuildScopedGraphProjector:
        """Bind a projector to a VALIDATED ``building`` build (§27.1).

        Anything else — the active build, another project's build, a finished
        snapshot, a typo'd id — raises the typed ``BuildNotWritableError``.
        """
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
        return BuildScopedGraphProjector(
            pg_conn, session, project, build_id, _token=_CONSTRUCTION_TOKEN
        )

    async def _assert_building(self) -> None:
        # FOR SHARE inside the caller's open Postgres transaction: the lock
        # outlives this check and the Neo4j write that follows it, and
        # conflicts with the activation UPDATE's row lock — so the write is
        # never astride an activation (a lock, not a smaller race window)
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

    async def project_entity(
        self,
        canonical_id: str,
        entity_type: str,
        status: str,
        name: str | None = None,
    ) -> None:
        """Upsert one entity node into the bound build (§4 node shape).

        MERGE keys on ``(canonical_id, build_id, project)`` so re-projection
        (pipeline retries, §5 idempotency) updates rather than duplicates.
        """
        await self._assert_building()
        await self._run(
            _PROJECT_ENTITY,
            {
                "canonical_id": canonical_id,
                "entity_type": entity_type,
                "status": status,
                "name": name,
            },
        )

    async def project_relation(self, src: str, dst: str, rel_type: str) -> None:
        """Upsert one ``src -[:REL {type}]-> dst`` edge inside the bound build.

        Both endpoints must already be projected IN THIS build — a missing
        endpoint raises :class:`RelationEndpointsNotProjectedError` instead of
        silently projecting nothing (§5 step order: entities before edges).
        """
        await self._assert_building()
        rows = await self._run(_PROJECT_RELATION, {"src": src, "dst": dst, "rel_type": rel_type})
        if not rows or rows[0]["linked"] == 0:
            raise RelationEndpointsNotProjectedError(self.project, self.build_id, src, dst)
