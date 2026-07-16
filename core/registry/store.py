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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.registry import jobs
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
    """delete_project on a project that still owns builds. Their multi-store
    cleanup (build-scoped rows + Neo4j/Qdrant projections) is the C9/BA8
    build-lifecycle's job, so we refuse rather than orphan them — a later
    project of the same name would otherwise resolve a STALE active build and
    serve old data. The ``builds.project`` FK (RESTRICT, BA2b) is the DB-level
    backstop; this typed error is the clean count-first path that also carries
    the count. Prune the builds first."""

    def __init__(self, name: str, count: int) -> None:
        super().__init__(f"project {name!r} still has {count} build(s); prune them first")
        self.name = name
        self.count = count


class ProjectHasActiveJobsError(Exception):
    """delete_project while a queued/running job is still in flight. A live job
    may not have created its build yet (so ProjectHasBuildsError wouldn't catch
    it), yet deleting the project out from under it would strand the operation
    (the jobs row would CASCADE away mid-run). Refuse until it finishes or is
    cancelled."""

    def __init__(self, name: str, count: int) -> None:
        super().__init__(f"project {name!r} has {count} active job(s); wait or cancel them first")
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

#: Reserved, SERVER-OWNED key under which an upload stashes its per-file DR-010
#: envelopes on the ONE managed text source (keyed by stored filename). Its PRESENCE
#: marks the source managed (its registered file list is authoritative — see
#: ``core.builds.sources._files_metadata`` / ``read_text_documents``). A dunder name
#: so it can't collide with a NON-upload text source's free-form ``metadata`` (which
#: the sources API stores verbatim): a plain source with a top-level ``files`` key is
#: legitimate project metadata and must still scan its directory, not be misread as an
#: upload manifest. Shared by the writer here and the build-time reader.
MANAGED_FILES_KEY = "__managed_files__"


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


# Non-build-scoped, project-keyed state with NO FK to projects (bare-text
# `project`), so it survives a plain projects-row delete and would silently
# carry forward onto a recreated same-name project — review decisions
# (DR-003), proposal decisions (DR-007), and observability runs (steps/items
# cascade off pipeline_runs by their own FKs). Purged in the delete txn.
_PROJECT_SCOPED_CARRYFORWARD = (
    tables.review_ledger,
    tables.ontology_proposals,
    tables.pipeline_runs,
)


async def delete_project(conn: AsyncConnection, name: str) -> bool:
    """Delete a project and its project-scoped Postgres state. Returns True if
    the project existed (and was removed), False if it did not. Raises
    ProjectHasBuildsError if the project still owns builds.

    Two tiers of project-scoped state, by cleanup cost:
    - builds (+ build-scoped rows + the Neo4j/Qdrant projections) need a
      multi-store sweep that is the C9 prune / BA8 lifecycle's job, so we
      REFUSE while any build exists — else a recreated same-name project would
      resolve a stale active build (active_build_id reads builds by
      project/status) and serve old data.
    - review_ledger / ontology_proposals / pipeline_runs are Postgres-only and
      carry forward by design across REBUILDS (DR-003/DR-007); on project
      DELETE that carry-forward is wrong (a new corpus under the same name
      would inherit old rejects/approvals), so we purge them here in the same
      transaction (bounded, single-store)."""
    # Lock the parent row FIRST (FOR UPDATE): a concurrent create_job/add_source
    # insert takes FOR KEY SHARE on this projects row for its FK check, so it now
    # blocks until this delete's transaction ends — closing the count-then-delete
    # TOCTOU where a job created after the count-returns-0 but before the DELETE
    # would be silently CASCADE-removed. NOTE builds.project has NO FK yet (BA2b
    # adds it), so a concurrent build insert is not serialized by this lock until
    # then; the builds-count refusal below is the interim guard for that path.
    # Absent row → nothing to delete.
    locked = (
        await conn.execute(
            sa.select(tables.projects.c.name)
            .where(tables.projects.c.name == name)
            .with_for_update()
        )
    ).one_or_none()
    if locked is None:
        return False
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
    active_jobs = await jobs.count_active_jobs(conn, name)
    if active_jobs > 0:
        raise ProjectHasActiveJobsError(name, active_jobs)
    for tbl in _PROJECT_SCOPED_CARRYFORWARD:
        await conn.execute(tbl.delete().where(tbl.c.project == name))
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


async def upsert_managed_source(
    conn: AsyncConnection,
    project: str,
    *,
    uri: str,
    kind: str,
    files: Mapping[str, dict[str, Any]],
) -> Source:
    """Register or update the ONE canonical managed source for a project (DR-010).

    The upload endpoint drops files into a per-project managed corpus directory
    and calls this to point a single ``file://`` source at that directory,
    stashing each accepted file's stored metadata envelope under
    ``metadata[MANAGED_FILES_KEY][<stored filename>]``. The ingest connector threads that
    envelope onto ``documents.metadata`` at build time (capture → persist), so
    this stash is the capture-to-build bridge, not the long-term home. Repeated
    uploads MERGE into the same source (by ``(project, uri)``) rather than mint a
    new one per upload — a project's managed corpus is one source. If duplicate
    rows already exist at that uri (the ``sources`` table has no ``(project, uri)``
    uniqueness), ALL are coalesced to the one canonical managed-text shape so none
    is left stale — ``list_sources`` feeds every matching row to the build.

    Serializes concurrent uploads to the same project by locking the project row
    (FOR UPDATE) before the find-or-create — the same row a concurrent insert
    takes FOR KEY SHARE — so two uploads can't each insert a managed source.
    Raises ProjectNotFoundError if the project is absent."""
    locked = (
        await conn.execute(
            sa.select(tables.projects.c.name)
            .where(tables.projects.c.name == project)
            .with_for_update()
        )
    ).one_or_none()
    if locked is None:
        raise ProjectNotFoundError(project)
    existing_rows = (
        await conn.execute(
            sa.select(*_SOURCE_COLS)
            .where(tables.sources.c.project == project, tables.sources.c.uri == uri)
            .order_by(tables.sources.c.added_at.asc(), tables.sources.c.id.asc())
        )
    ).all()
    if not existing_rows:
        row = (
            await conn.execute(
                tables.sources.insert()
                .values(
                    project=project, kind=kind, uri=uri, metadata={MANAGED_FILES_KEY: dict(files)}
                )
                .returning(*_SOURCE_COLS)
            )
        ).one()
        return Source(*row)
    # Coalesce EVERY row at (project, uri), not just the oldest. The table has no
    # (project, uri) uniqueness, so a project can hold duplicate managed-corpus
    # rows, and list_sources feeds ALL of them to the build. A stale duplicate left
    # behind would corrupt or break an otherwise-correct upload: a fileless text row
    # directory-scans the corpus and persists FALLBACK metadata, and a non-text row
    # fails the build in resolve_source. So merge the files of all matching rows with
    # the new ones (union) and rewrite EVERY matching row to the one canonical
    # managed-text shape — kind=text, metadata={MANAGED_FILES_KEY: …}, exactly what a
    # fresh insert writes. Non-managed metadata on a stale row is dropped: it is inert
    # for the text connector and this IS the canonical managed form. A row with NO
    # managed stash (a plain/fileless or non-text duplicate) contributes nothing and is
    # simply coalesced. But a row whose MANAGED_FILES_KEY is PRESENT-but-non-object is
    # MALFORMED: _files_metadata fails LOUD on exactly that at read time, so the write
    # path must not silently ERASE it by rewriting to a fresh map (that could change
    # which files a build ingests). Malformed per-file ENTRIES *within* a dict stash are
    # still carried forward — _files_metadata raises on those at read time, loud.
    merged_files: dict[str, dict[str, Any]] = {}
    for existing in existing_rows:
        metadata = Source(*existing).metadata
        if MANAGED_FILES_KEY not in metadata:
            continue
        prior = metadata[MANAGED_FILES_KEY]
        if not isinstance(prior, dict):
            raise ValueError(
                f"managed source at {uri!r} has a non-object {MANAGED_FILES_KEY!r} metadata "
                f"value ({type(prior).__name__}); refusing to coalesce over a malformed "
                "managed marker — a present key marks the source managed and must be an object"
            )
        merged_files.update(prior)
    merged_files.update(files)
    await conn.execute(
        tables.sources.update()
        .where(tables.sources.c.project == project, tables.sources.c.uri == uri)
        .values(kind=kind, metadata={MANAGED_FILES_KEY: merged_files})
    )
    # return the canonical (oldest) row, re-read to reflect the coalescing update
    canonical_id = Source(*existing_rows[0]).id
    row = (
        await conn.execute(sa.select(*_SOURCE_COLS).where(tables.sources.c.id == canonical_id))
    ).one()
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
