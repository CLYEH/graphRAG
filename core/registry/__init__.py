"""Control-plane registry (BA1): projects and their data sources.

The non-build-scoped face over `tables.projects` / `tables.sources` — plain
functions over an ``AsyncConnection`` (mirroring ``core.builds.lifecycle``),
NOT the build-scoped repo (which rejects these tables). Consumed by the
Console API routers (BA1b); the ingest/build triggers and idempotency store
land in BA1b/BA2.
"""

from core.registry.store import (
    Project,
    ProjectExistsError,
    ProjectNotFoundError,
    Source,
    add_source,
    create_project,
    delete_project,
    get_project,
    list_projects,
    list_sources,
    update_project,
)

__all__ = [
    "Project",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "Source",
    "add_source",
    "create_project",
    "delete_project",
    "get_project",
    "list_projects",
    "list_sources",
    "update_project",
]
