"""Build-scoped repository over Postgres (DESIGN §27.1, DR-001/DR-006, C1b).

The structural guarantee DR-006 promises: query/MCP/api layers never touch a
raw store client, so they *cannot* forget a ``build_id`` filter and mix
versions. This module is that structure for Postgres:

- ``active_build_id`` — DR-001's single source of truth, one query against
  ``builds.status='active'`` (the partial unique index guarantees at most one
  row). No active build raises the typed ``NoActiveBuildError``, which the
  API layer maps to the frozen ``NO_ACTIVE_BUILD`` error code (§15).
- ``BuildScopedRepo`` — binds ``(project, build_id)`` once at construction
  (§27.1: read the active id once per request and cache it — the bound repo
  IS that cache) and injects them into every read and write. Reads via
  :meth:`select` return a ``Select`` already filtered; writes via
  :meth:`insert_values` get the scope columns injected, so a caller cannot
  write another build's rows.
- The scope column map is explicit: tables that are deliberately NOT
  build-scoped (``builds`` itself, ``review_ledger`` per DR-003, the
  observability tables with their own §27.7 binding rules) and tables scoped
  only transitively (``entity_mentions`` hang off entities, §4 gives them no
  build_id) are rejected loudly — silently "scoping" them would fake the
  guarantee this layer exists to give.

Neo4j/Qdrant projections get the same treatment in C1c/C1d (DR-004's
``WHERE n.build_id`` / payload filter); this module is Postgres-only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BuildScopedRepo:
    """Postgres access bound to one ``(project, build_id)`` (DR-006).

    Frozen on purpose: the binding is the §27.1 per-request cache, and a repo
    whose scope could be mutated mid-request would reintroduce exactly the
    mixed-version reads this layer exists to prevent. Construct via
    :meth:`for_active_build` for queries (scope = the active build) or bind a
    ``building`` build's id explicitly for pipeline writes (§27.1: 寫入一律指定
    building 的 build_id).
    """

    conn: AsyncConnection
    project: str
    build_id: uuid.UUID

    @classmethod
    async def for_active_build(cls, conn: AsyncConnection, project: str) -> BuildScopedRepo:
        """Bind to the project's active build (one lookup, then cached here)."""
        return cls(conn=conn, project=project, build_id=await active_build_id(conn, project))

    def _scope_columns(self, table: sa.Table) -> tuple[str, ...]:
        try:
            return _SCOPE_COLUMNS[table]
        except KeyError:
            raise NotBuildScopedError(
                f"table {table.name!r} is not directly build-scoped — "
                "see core.stores.repo docstring for why it is excluded"
            ) from None

    def _scope_values(self, table: sa.Table) -> dict[str, Any]:
        values: dict[str, Any] = {"build_id": self.build_id}
        if "project" in self._scope_columns(table):
            values["project"] = self.project
        return values

    def select(self, table: sa.Table) -> sa.Select[Any]:
        """A ``SELECT`` with the scope filters already injected.

        Callers add their own predicates on top; they cannot remove these.
        """
        query = sa.select(table)
        for column, value in self._scope_values(table).items():
            query = query.where(table.c[column] == value)
        return query

    def insert_values(self, table: sa.Table, /, **values: Any) -> sa.Insert:
        """An ``INSERT`` with the scope columns injected.

        A caller-supplied conflicting scope value is a bug by definition —
        rejected loudly rather than silently overwritten (either direction
        would hide a cross-build write).
        """
        scope = self._scope_values(table)
        conflicts = {
            key: values[key] for key in scope.keys() & values.keys() if values[key] != scope[key]
        }
        if conflicts:
            raise ValueError(
                f"scope columns {sorted(conflicts)} conflict with this repo's binding "
                f"(project={self.project!r}, build_id={self.build_id!r})"
            )
        return table.insert().values({**values, **scope})
