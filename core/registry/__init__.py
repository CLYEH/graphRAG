"""Control-plane registry (BA1): projects and their data sources.

The non-build-scoped face over `tables.projects` / `tables.sources` — plain
functions over an ``AsyncConnection`` (mirroring ``core.builds.lifecycle``),
NOT the build-scoped repo (which rejects these tables). Consumed by the
Console API routers (BA1b); the ingest/build triggers and idempotency store
land in BA1b/BA2.
"""

from core.registry.jobs import (
    Job,
    JobConflictError,
    JobNotFoundError,
    capture_config_snapshot,
    count_active_jobs,
    create_job,
    create_job_exclusive,
    find_reapable_jobs,
    find_unenqueued_jobs,
    get_job,
    get_job_at,
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
    upsert_managed_source,
)

__all__ = [
    "Job",
    "JobConflictError",
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
    "create_job_exclusive",
    "create_project",
    "delete_project",
    "find_reapable_jobs",
    "find_unenqueued_jobs",
    "get_job",
    "get_job_at",
    "get_project",
    "is_cancel_requested",
    "list_projects",
    "list_sources",
    "request_cancel",
    "set_progress",
    "update_project",
    "upsert_managed_source",
]
