"""BA2d-3: partial index for the reaper scan — jobs_reapable.

The lease reaper cron runs twice a minute matching "held lease + expired + job
still active" (find_reapable_jobs). Without a supporting index that's a seq scan
over the whole job history every 30s; jobs_by_project (project, created_at) can't
serve it. This partial index covers exactly the held-active-lease set — near-empty
in a healthy system (only in-flight dispatches, and only crashed ones stay past
their TTL) — so each tick is a cheap index probe. The WHERE mirrors the reaper's
predicate byte-for-byte.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_jobs_reaper_index"
down_revision = "0012_jobs_config_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "jobs_reapable",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_owner IS NOT NULL AND status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("jobs_reapable", table_name="jobs")
