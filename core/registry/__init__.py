"""Control-plane registry (BA1): projects and their data sources.

The non-build-scoped face over `tables.projects` / `tables.sources` — plain
functions over an ``AsyncConnection`` (mirroring ``core.builds.lifecycle``),
NOT the build-scoped repo (which rejects these tables). Consumed by the
Console API routers (BA1b); the ingest/build triggers and idempotency store
land in BA1b/BA2.
"""

from core.registry.jobs import (
    Job,
    JobNotFoundError,
    capture_config_snapshot,
    count_active_jobs,
    create_job,
    find_reapable_jobs,
    get_job,
    is_cancel_requested,
    request_cancel,
    set_progress,
)
from core.registry.store import (
    Project,
    ProjectExistsError,
    ProjectHasActiveJobsError,
    ProjectHasBuildsError,
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
    "Job",
    "JobNotFoundError",
    "Project",
    "ProjectExistsError",
    "ProjectHasActiveJobsError",
    "ProjectHasBuildsError",
    "ProjectNotFoundError",
    "Source",
    "add_source",
    "capture_config_snapshot",
    "count_active_jobs",
    "create_job",
    "create_project",
    "delete_project",
    "find_reapable_jobs",
    "get_job",
    "get_project",
    "is_cancel_requested",
    "list_projects",
    "list_sources",
    "request_cancel",
    "set_progress",
    "update_project",
]
