"""Projects/sources registry access (BA1) — the control-plane CRUD.

Plain async functions over an ``AsyncConnection`` and SQLAlchemy Core, the
same non-build-scoped face ``core.builds.lifecycle`` uses (the build-scoped
repo deliberately rejects these tables). No HTTP concerns here: list functions
take/return a keyset ``after`` tuple, not an opaque cursor — the router (BA1b)
owns the opaque-token encoding and the §15 envelope. Domain errors
(ProjectExistsError / ProjectNotFoundError) are raised for the router to map
to the frozen error codes; SQL never leaks upward.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables


@dataclass(frozen=True)
class Project:
    """A control-plane project. ``name`` is the stable key used in API paths
    and store scoping; ``config`` is always a dict (never null)."""

    name: str
    display_name: str | None
    description: str | None
    config: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class Source:
    """A registered data source under a project. ``metadata`` is always a
    dict; ``kind`` is optional (the connector kind, e.g. file/url/database)."""

    id: uuid.UUID
    project: str
    kind: str | None
    uri: str
    metadata: dict[str, Any]
    added_at: datetime


class ProjectExistsError(Exception):
    """create_project on a name that already exists (PK conflict)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} already exists")
        self.name = name


class ProjectNotFoundError(Exception):
    """add_source (or any child write) referencing a project that is absent."""

    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} does not exist")
        self.name = name


class ProjectHasBuildsError(Exception):
    """delete_project on a project that still owns builds. ``builds.project``
    is bare text (no FK — builds predate the registry and their multi-store
    cleanup is the C9/BA8 build-lifecycle's job), so deleting the project row
    would leave its builds and build-scoped data keyed by the same name; a
    later project of the same name would then resolve a STALE active build and
    serve old data. Refusing here closes that hole structurally — prune the
    builds first."""

    def __init__(self, name: str, count: int) -> None:
        super().__init__(f"project {name!r} still has {count} build(s); prune them first")
        self.name = name
        self.count = count


class _Unset:
    """PATCH sentinel — distinguishes 'field omitted' from 'set to null'."""


_UNSET: Any = _Unset()

# Postgres SQLSTATEs we attribute to a specific domain error; anything else
# (CHECK 23514, NOT NULL 23502, …) re-raises so a real constraint bug fails
# loud instead of being mislabeled as "exists"/"not found".
_SQLSTATE_UNIQUE = "23505"
_SQLSTATE_FK = "23503"


def _sqlstate(exc: IntegrityError) -> str | None:
    """The Postgres SQLSTATE behind a SQLAlchemy IntegrityError (asyncpg puts
    it on ``exc.orig.sqlstate``), or None if unavailable."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _patch_values(
    display_name: str | None | _Unset,
    description: str | None | _Unset,
    config: dict[str, Any] | _Unset,
) -> dict[str, Any]:
    """The PATCH's SET clause — only fields that were passed (not left at
    _UNSET) appear; a passed None stays in the dict (→ column set null). Pure
    so the omitted-vs-null distinction is unit-tested without a DB."""
    values: dict[str, Any] = {}
    if not isinstance(display_name, _Unset):
        values["display_name"] = display_name
    if not isinstance(description, _Unset):
        values["description"] = description
    if not isinstance(config, _Unset):
        values["config"] = config
    return values


_PROJECT_COLS = (
    tables.projects.c.name,
    tables.projects.c.display_name,
    tables.projects.c.description,
    tables.projects.c.config,
    tables.projects.c.created_at,
)
_SOURCE_COLS = (
    tables.sources.c.id,
    tables.sources.c.project,
    tables.sources.c.kind,
    tables.sources.c.uri,
    tables.sources.c.metadata,
    tables.sources.c.added_at,
)


async def create_project(
    conn: AsyncConnection,
    *,
    name: str,
    display_name: str | None = None,
    description: str | None = None,
    config: dict[str, Any] | None = None,
) -> Project:
    """Insert a project, returning it. Raises ProjectExistsError if the name
    is taken (the PK conflict, mapped by the router to a 409)."""
    try:
        row = (
            await conn.execute(
                tables.projects.insert()
                .values(
                    name=name,
                    display_name=display_name,
                    description=description,
                    config=config if config is not None else {},
                )
                .returning(*_PROJECT_COLS)
            )
        ).one()
    except IntegrityError as exc:
        if _sqlstate(exc) == _SQLSTATE_UNIQUE:  # only the name PK conflict is "exists"
            raise ProjectExistsError(name) from exc
        raise  # a CHECK/other violation is a real bug — fail loud, don't mislabel
    return Project(*row)


async def get_project(conn: AsyncConnection, name: str) -> Project | None:
    row = (
        await conn.execute(sa.select(*_PROJECT_COLS).where(tables.projects.c.name == name))
    ).one_or_none()
    return Project(*row) if row is not None else None


async def list_projects(
    conn: AsyncConnection,
    *,
    limit: int,
    after: tuple[datetime, str] | None = None,
) -> tuple[list[Project], tuple[datetime, str] | None]:
    """One page of projects, newest first (created_at desc, name desc as the
    stable tiebreak). ``after`` is the keyset of the last row on the previous
    page; the returned tuple's second element is the keyset to resume from, or
    None on the last page. Fetches limit+1 to detect a further page without a
    second query."""
    key = sa.tuple_(tables.projects.c.created_at, tables.projects.c.name)
    query = sa.select(*_PROJECT_COLS).order_by(
        tables.projects.c.created_at.desc(), tables.projects.c.name.desc()
    )
    if after is not None:
        query = query.where(key < sa.tuple_(sa.literal(after[0]), sa.literal(after[1])))
    rows = (await conn.execute(query.limit(limit + 1))).all()
    projects = [Project(*r) for r in rows[:limit]]
    # `and projects` guards limit=0 (rows[:0] is empty though a row was fetched)
    next_after = (
        (projects[-1].created_at, projects[-1].name) if len(rows) > limit and projects else None
    )
    return projects, next_after


async def update_project(
    conn: AsyncConnection,
    name: str,
    *,
    display_name: str | None | _Unset = _UNSET,
    description: str | None | _Unset = _UNSET,
    config: dict[str, Any] | _Unset = _UNSET,
) -> Project | None:
    """Apply a PATCH — only the fields passed (not left at _UNSET) change; a
    passed None sets the column null. Returns the updated project, or None if
    it does not exist. An empty patch is a no-op read."""
    values = _patch_values(display_name, description, config)
    if not values:
        return await get_project(conn, name)
    row = (
        await conn.execute(
            tables.projects.update()
            .where(tables.projects.c.name == name)
            .values(**values)
            .returning(*_PROJECT_COLS)
        )
    ).one_or_none()
    return Project(*row) if row is not None else None


async def delete_project(conn: AsyncConnection, name: str) -> bool:
    """Delete a project (its sources cascade via the FK). Returns True if a
    row was removed, False if the project did not exist. Raises
    ProjectHasBuildsError if the project still owns builds — deleting it would
    strand build-scoped data under a reusable name (stale active build on
    recreate); prune the builds via the lifecycle first."""
    builds_count = int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(tables.builds)
                .where(tables.builds.c.project == name)
            )
        ).scalar_one()
    )
    if builds_count > 0:
        raise ProjectHasBuildsError(name, builds_count)
    result = await conn.execute(tables.projects.delete().where(tables.projects.c.name == name))
    return result.rowcount > 0


async def add_source(
    conn: AsyncConnection,
    project: str,
    *,
    uri: str,
    kind: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Source:
    """Register a source under a project, returning it. Raises
    ProjectNotFoundError if the project is absent (checked explicitly so the
    router gets a clean 404; the FK backstops a concurrent delete)."""
    if await get_project(conn, project) is None:
        raise ProjectNotFoundError(project)
    try:
        row = (
            await conn.execute(
                tables.sources.insert()
                .values(
                    project=project,
                    kind=kind,
                    uri=uri,
                    metadata=metadata if metadata is not None else {},
                )
                .returning(*_SOURCE_COLS)
            )
        ).one()
    except IntegrityError as exc:
        # the project vanished between the check and the insert (FK violation);
        # any other integrity error is a real bug, so re-raise it
        if _sqlstate(exc) == _SQLSTATE_FK:
            raise ProjectNotFoundError(project) from exc
        raise
    return Source(*row)


async def list_sources(
    conn: AsyncConnection,
    project: str,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> tuple[list[Source], tuple[datetime, uuid.UUID] | None]:
    """One page of a project's sources, newest first (added_at desc, id desc
    tiebreak). Same keyset contract as list_projects."""
    key = sa.tuple_(tables.sources.c.added_at, tables.sources.c.id)
    query = (
        sa.select(*_SOURCE_COLS)
        .where(tables.sources.c.project == project)
        .order_by(tables.sources.c.added_at.desc(), tables.sources.c.id.desc())
    )
    if after is not None:
        query = query.where(key < sa.tuple_(sa.literal(after[0]), sa.literal(after[1])))
    rows = (await conn.execute(query.limit(limit + 1))).all()
    src = [Source(*r) for r in rows[:limit]]
    next_after = (src[-1].added_at, src[-1].id) if len(rows) > limit and src else None
    return src, next_after
