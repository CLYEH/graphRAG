"""BA2d-2: pin a build's config — jobs.config_snapshot.

run_build_task would otherwise re-read the mutable project config on every
dispatch, but a job can run more than once — a PATCH /projects during the queue
delay before the first dispatch, an arq retry after a transient error, or the
BA2d-3 reaper re-enqueuing a crashed build. Since PATCH /projects does not block
active jobs, that would drift a resuming build's chunking / ontology params —
breaking convergent idempotency or mixing outputs across the change. This nullable
JSONB pins the config the user submitted: create_job captures it at job creation,
and every (re-)dispatch reuses it (the worker's capture_config_snapshot reads it
back, defensively pinning live config only for a job that somehow lacks one).
Nullable + no backfill — pre-existing jobs simply have no snapshot.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_jobs_config_snapshot"
down_revision = "0011_jobs_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("config_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "config_snapshot")
