"""BA2d-2: pin a build's config — jobs.config_snapshot.

run_build_task loads the project config on every dispatch, but a job can be
dispatched more than once — an arq retry after a transient error, or the BA2d-3
reaper re-enqueuing a crashed build. PATCH /projects does not block active jobs,
so a mid-build config edit would otherwise drift a resuming build's chunking /
ontology params — breaking convergent idempotency or mixing outputs across the
change. This nullable JSONB pins the raw config the build started with: the
worker sets it on the first dispatch (a COALESCE UPDATE) and reads it back on
every later one. Nullable + no backfill — a job not yet dispatched simply has no
snapshot, and the first dispatch captures it.
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
