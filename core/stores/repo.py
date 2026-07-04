"""Build-scoped repository over Postgres (DESIGN §27.1, DR-001/DR-006, C1b).

The structural guarantee DR-006 promises: query/MCP/api layers never touch a
raw store client, so they *cannot* forget a ``build_id`` filter and mix
versions. This module is that structure for Postgres:

- ``active_build_id`` — DR-001's single source of truth, one query against
  ``builds.status='active'`` (the partial unique index guarantees at most one
  row). No active build raises the typed ``NoActiveBuildError``, which the
  API layer maps to the frozen ``NO_ACTIVE_BUILD`` error code (§15).
- ``BuildScopedRepo`` — the READ capability: binds ``(project, build_id)``
  once at construction (§27.1: read the active id once per request and cache
  it — the bound repo IS that cache; no setters, so the scope cannot drift
  mid-request) and injects the scope into every read. The repo EXECUTES
  internally (:meth:`fetch_all`): consumers are never handed the raw
  connection, because a repo that only built statements would force callers
  to hold a connection to run them — making the DR-006 bypass the normal
  path instead of a fenced-off one. The connection attribute is name-mangled
  private; reaching it is a deliberate act, not a convenience.
- ``BuildScopedWriter`` — the WRITE capability, a separate type: §27.1 writes
  always target a ``building`` build, so :meth:`~BuildScopedWriter.insert`
  exists ONLY on instances that came through the validating
  :meth:`~BuildScopedWriter.for_building_build` factory. An active-bound repo
  has no insert method to misuse — the live snapshot's immutability is a
  property of the type, not a runtime flag.
- The scope column map is explicit: tables that are deliberately NOT
  build-scoped (``builds`` itself, ``review_ledger`` per DR-003, the
  observability tables with their own §27.7 binding rules) and tables scoped
  only transitively (``entity_mentions`` hang off entities, §4 gives them no
  build_id) are rejected loudly — silently "scoping" them would fake the
  guarantee this layer exists to give.

The read/write surface is deliberately minimal (filtered fetch, scoped
insert); C4/C6/BA3 extend it additively as their access patterns land.
Neo4j/Qdrant projections get the same treatment in C1c/C1d (DR-004's
``WHERE n.build_id`` / payload filter); this module is Postgres-only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables

#: §4 core tables carrying BOTH scope columns — reads filter and writes inject
#: (project, build_id).
PROJECT_AND_BUILD_SCOPED = (
    tables.documents,
    tables.entities,
    tables.relations,
    tables.community_reports,
    tables.merge_candidates,
)

#: §4 core tables carrying only build_id (their project is derivable through
#: the composite FK parent) — reads filter and writes inject build_id.
BUILD_ONLY_SCOPED = (
    tables.chunks,
    tables.relation_evidence,
)

_SCOPE_COLUMNS: dict[sa.Table, tuple[str, ...]] = {
    **{table: ("project", "build_id") for table in PROJECT_AND_BUILD_SCOPED},
    **{table: ("build_id",) for table in BUILD_ONLY_SCOPED},
}


class NotBuildScopedError(TypeError):
    """Raised when a table must not be silently build-scoped.

    ``builds`` is the scope's source of truth, ``review_ledger`` is
    deliberately cross-build (DR-003), the observability tables have their own
    §27.7 binding rules, and ``entity_mentions`` are scoped transitively
    through their entity (§4 gives them no build_id column). Pretending to
    scope any of these would fake the DR-006 guarantee.
    """


class NoActiveBuildError(LookupError):
    """No ``builds.status='active'`` row for the project (DR-001).

    The API layer maps this to the frozen ``NO_ACTIVE_BUILD`` error code
    (§15 / §27.2); core raises it typed instead of guessing a build.
    """

    def __init__(self, project: str) -> None:
        super().__init__(f"no active build for project {project!r}")
        self.project = project


class BuildNotWritableError(LookupError):
    """The requested write binding is not this project's ``building`` build.

    §27.1: 寫入一律指定 building 的 build_id — every other status is an
    immutable snapshot (writing into ``active`` would mutate live data;
    another project's build would cross scopes). ``status`` is None when the
    build id does not exist at all.
    """

    def __init__(self, project: str, build_id: uuid.UUID, status: str | None) -> None:
        super().__init__(
            f"build {build_id} is not a 'building' build of project {project!r} "
            f"(found status: {status!r})"
        )
        self.project = project
        self.build_id = build_id
        self.status = status


#: Module-private construction token: the factories below are the only
#: sanctioned ways to bind a scope (the active build for reads, a VALIDATED
#: building build for writes) — an unvalidated direct construction would
#: reopen the bind-to-anything hole.
_CONSTRUCTION_TOKEN = object()


async def active_build_id(conn: AsyncConnection, project: str) -> uuid.UUID:
    """DR-001: the single-query active-build lookup (§27.1).

    At most one row can match — ``one_active_build`` is a partial unique
    index, so "the" active build is a database invariant, not a convention.
    """
    result = (
        await conn.execute(
            sa.select(tables.builds.c.id).where(
                tables.builds.c.project == project,
                tables.builds.c.status == "active",
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise NoActiveBuildError(project)
    build: uuid.UUID = result
    return build


class BuildScopedRepo:
    """Read-only Postgres access bound to one ``(project, build_id)`` (DR-006).

    Construct via :meth:`for_active_build` (the normal query/Console path).
    The scope is read-only after construction, and execution happens inside
    the repo — a consumer holding a repo holds no raw connection to escape
    through, and no write method exists on this type (see
    :class:`BuildScopedWriter`).
    """

    __slots__ = ("__conn", "__project", "__build_id")

    def __init__(
        self,
        conn: AsyncConnection,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError(
                "construct via BuildScopedRepo.for_active_build (reads) or "
                "BuildScopedWriter.for_building_build (pipeline writes) — direct "
                "construction would skip the scope validation those factories do"
            )
        # name-mangled: reaching the connection from outside is a deliberate
        # bypass (visible in review), never an accident of convenience
        self.__conn = conn
        self.__project = project
        self.__build_id = build_id

    @property
    def project(self) -> str:
        return self.__project

    @property
    def build_id(self) -> uuid.UUID:
        return self.__build_id

    @classmethod
    async def for_active_build(cls, conn: AsyncConnection, project: str) -> BuildScopedRepo:
        """Bind to the project's active build (one lookup, then cached here).

        Always returns the read-only type — pinned explicitly (not ``cls``)
        so a subclass can never be tricked into an active-bound WRITER.
        """
        build = await active_build_id(conn, project)
        return BuildScopedRepo(conn, project, build, _token=_CONSTRUCTION_TOKEN)

    # -- scope plumbing (single-underscore: shared with the SQL-shape tests) --

    def _scope_columns(self, table: sa.Table) -> tuple[str, ...]:
        try:
            return _SCOPE_COLUMNS[table]
        except KeyError:
            raise NotBuildScopedError(
                f"table {table.name!r} is not directly build-scoped — "
                "see core.stores.repo docstring for why it is excluded"
            ) from None

    def _scope_values(self, table: sa.Table) -> dict[str, Any]:
        values: dict[str, Any] = {"build_id": self.__build_id}
        if "project" in self._scope_columns(table):
            values["project"] = self.__project
        return values

    def _select(self, table: sa.Table) -> sa.Select[Any]:
        query = sa.select(table)
        for column, value in self._scope_values(table).items():
            query = query.where(table.c[column] == value)
        return query

    def _insert_values(self, table: sa.Table, values: dict[str, Any]) -> sa.Insert:
        scope = self._scope_values(table)
        conflicts = {
            key: values[key] for key in scope.keys() & values.keys() if values[key] != scope[key]
        }
        if conflicts:
            raise ValueError(
                f"scope columns {sorted(conflicts)} conflict with this repo's binding "
                f"(project={self.__project!r}, build_id={self.__build_id!r})"
            )
        return table.insert().values({**values, **scope})

    async def _execute(self, statement: sa.Executable) -> sa.CursorResult[Any]:
        # the sole execution seam — defined here because the mangled __conn is
        # unreachable from subclasses (that unreachability is the point)
        return await self.__conn.execute(statement)

    # -- the public, executing surface ----------------------------------------

    async def fetch_all(
        self, table: sa.Table, *where: sa.ColumnExpressionArgument[bool]
    ) -> Sequence[sa.Row[Any]]:
        """Read the scoped rows, optionally narrowed by caller predicates.

        Predicates can only narrow — the scope filters are already in the
        query and nothing the caller passes can remove them.
        """
        query = self._select(table)
        for predicate in where:
            query = query.where(predicate)
        return (await self._execute(query)).fetchall()


class BuildScopedWriter(BuildScopedRepo):
    """The pipeline write capability (§27.1: writes target a building build).

    Exists ONLY via :meth:`for_building_build` — the validating factory is
    the type's sole entry, so "this object can insert" and "this scope is a
    verified building build of this project" are the same fact. Writers also
    read (their own build: skip/rerun decisions need it).
    """

    __slots__ = ()

    @classmethod
    async def for_building_build(
        cls, conn: AsyncConnection, project: str, build_id: uuid.UUID
    ) -> BuildScopedWriter:
        """Bind a pipeline writer to a VALIDATED ``building`` build (§27.1).

        The id must name an existing build, of THIS project, in status
        ``building`` — anything else (the active build, another project's
        build, a finished snapshot, a typo'd id) raises the typed
        ``BuildNotWritableError`` instead of silently landing writes where
        they would mutate live data or cross scopes.
        """
        status = (
            await conn.execute(
                sa.select(tables.builds.c.status).where(
                    tables.builds.c.id == build_id,
                    tables.builds.c.project == project,
                )
            )
        ).scalar_one_or_none()
        if status != "building":
            raise BuildNotWritableError(project, build_id, status)
        return BuildScopedWriter(conn, project, build_id, _token=_CONSTRUCTION_TOKEN)

    async def insert(self, table: sa.Table, /, **values: Any) -> None:
        """Insert a row with the scope columns injected.

        A caller-supplied conflicting scope value is a bug by definition —
        rejected loudly rather than silently overwritten (either direction
        would hide a cross-build write).
        """
        await self._execute(self._insert_values(table, values))
