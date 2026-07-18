"""The ONE place registry domain errors become frozen-envelope ApiErrors (BA1b).

Single translation point so that if the owner approves a dedicated 409 conflict
code (see the GAP note below), only this file changes.

GAP (DR-002 / owner decision): the frozen ErrorCode enum has PROJECT_NOT_FOUND
but NO "already exists" / generic-conflict code, and the contract's 409 Conflict
set is exhausted by IDEMPOTENCY_CONFLICT/JOB_CONFLICT/BUILD_NOT_READY/
NO_ACTIVE_BUILD — none fits "project name taken" or "project still has builds".
Reusing one of those would mislead a client dispatching on error.code, and
inventing a code would breach DR-002 (the same call the auth placeholder makes).
So both are surfaced as VALIDATION_ERROR (400) with machine-readable details,
pending a DR-002 proposal to add e.g. a CONFLICT (409) code. When that lands,
only the two lines below change.
"""

from __future__ import annotations

from api.errors import ApiError, ErrorCode
from core.graph.proposals import OntologyProposalNotFoundError
from core.registry import (
    JobConflictError,
    JobNotFoundError,
    ProjectExistsError,
    ProjectHasActiveJobsError,
    ProjectHasBuildsError,
    ProjectNotFoundError,
    SourceNotFoundError,
)
from core.stores.repo import NoActiveBuildError


def translate_registry_error(exc: Exception) -> ApiError:
    """Map a known registry domain error to an ApiError; re-raise anything
    else (an unexpected error must not be silently reshaped into a 4xx)."""
    if isinstance(exc, ProjectNotFoundError):
        return ApiError(ErrorCode.PROJECT_NOT_FOUND, str(exc), details={"project": exc.name})
    if isinstance(exc, JobNotFoundError):
        return ApiError(ErrorCode.JOB_NOT_FOUND, str(exc), details={"job_id": exc.job_id})
    if isinstance(exc, SourceNotFoundError):
        return ApiError(
            ErrorCode.SOURCE_NOT_FOUND,
            str(exc),
            details={"project": exc.project, "source_id": str(exc.source_id)},
        )
    if isinstance(exc, OntologyProposalNotFoundError):
        return ApiError(
            ErrorCode.PROPOSAL_NOT_FOUND,
            str(exc),
            details={"project": exc.project, "proposal_id": str(exc.proposal_id)},
        )
    if isinstance(exc, JobConflictError):
        return ApiError(
            ErrorCode.JOB_CONFLICT,
            str(exc),
            details={"project": exc.project, "active_job_id": exc.active_job_id},
        )
    # a DR-006 repo error, not a registry one — but this stays the single
    # domain-error → frozen-code translation point (module docstring)
    if isinstance(exc, NoActiveBuildError):
        return ApiError(ErrorCode.NO_ACTIVE_BUILD, str(exc), details={"project": exc.project})
    if isinstance(exc, ProjectExistsError):
        return ApiError(
            ErrorCode.VALIDATION_ERROR,  # GAP: no frozen "exists"/conflict code
            f"project {exc.name!r} already exists",
            details={"name": exc.name},
        )
    if isinstance(exc, ProjectHasBuildsError):
        return ApiError(
            ErrorCode.VALIDATION_ERROR,  # GAP: no frozen "has builds"/conflict code
            str(exc),
            details={"project": exc.name, "builds": exc.count},
        )
    if isinstance(exc, ProjectHasActiveJobsError):
        return ApiError(
            ErrorCode.VALIDATION_ERROR,  # GAP: no frozen "active jobs"/conflict code
            str(exc),
            details={"project": exc.name, "jobs": exc.count},
        )
    raise exc
