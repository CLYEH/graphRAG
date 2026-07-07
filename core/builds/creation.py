"""Registry-aware build creation (BA2c) — minting a fresh ``building`` build.

`core/builds/lifecycle.py` owns the admin surface over EXISTING builds
(activate/rollback/diff/prune, always by explicit id); creation — inserting the
row in the first place — is a distinct concern and lives here, its sibling under
the same `builds/` package (no new top-level `core/` dir; §12's layout is kept).

Registry-aware (BA2b): the `builds.project → projects.name` RESTRICT FK means a
build cannot exist without its project. Creation checks the project explicitly
(so a bad name raises a clean typed ``ProjectNotFoundError`` for the router to
map to 404, rather than a raw FK violation), and the FK backstops the concurrent
project-delete race — the exact shape ``core.registry.store.add_source`` uses.
"Ensure the project exists" means VERIFY, not upsert: a project is created only
via ``create_project`` (BA1); creation never auto-mints one.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.registry.store import ProjectNotFoundError, get_project
from core.stores import tables

#: Postgres SQLSTATE for a foreign-key violation (mirrors the private constant
#: in core.registry.store; the FK backstops a delete that races the check).
_SQLSTATE_FK = "23503"


async def create_build(
    conn: AsyncConnection,
    project: str,
    *,
    config_hash: str | None = None,
    source_hash: str | None = None,
) -> uuid.UUID:
    """Insert a fresh ``building`` build for ``project`` and return its id.

    Raises ``ProjectNotFoundError`` if the project is absent (checked
    explicitly for a clean 404; the FK backstops a concurrent delete). Does NOT
    commit — the caller (the orchestrator) owns the transaction boundary, like
    ``create_project``/``add_source``. ``started_at`` is the DB clock
    (``now()``, the single-clock-source rule) so the lifecycle's
    ``started_at``-desc ordering stays monotonic.
    """
    if await get_project(conn, project) is None:
        raise ProjectNotFoundError(project)
    try:
        build_id: uuid.UUID = (
            await conn.execute(
                tables.builds.insert()
                .values(
                    project=project,
                    status="building",
                    config_hash=config_hash,
                    source_hash=source_hash,
                    started_at=sa.func.now(),
                )
                .returning(tables.builds.c.id)
            )
        ).scalar_one()
    except IntegrityError as exc:
        # the project vanished between the check and the insert (FK violation);
        # any other integrity error is a real bug, so re-raise it
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == _SQLSTATE_FK:
            raise ProjectNotFoundError(project) from exc
        raise
    return build_id
