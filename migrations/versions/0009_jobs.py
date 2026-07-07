"""BA2a: jobs — the §15/§27.7 long-operation tracking table.

The durable SoR the Console serves for GET /jobs/{id}; arq+Redis is only the
execution queue. `project` CASCADE-deletes with its project (pure execution
audit, no cross-build carry-forward). `build_id` is nullable and deliberately
NOT an FK (mirrors pipeline_runs). New table, no backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_jobs"
down_revision = "0008_idempotency_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project",
            sa.Text,
            sa.ForeignKey("projects.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("step", sa.Text),
        sa.Column("progress", sa.REAL, nullable=False, server_default=sa.text("0")),
        sa.Column("message", sa.Text),
        sa.Column("error", postgresql.JSONB),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled')", name="jobs_status_valid"
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="jobs_progress_bounded"),
        sa.CheckConstraint("kind <> ''", name="jobs_kind_nonempty"),
    )
    op.create_index("jobs_by_project", "jobs", ["project", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("jobs_by_project", table_name="jobs")
    op.drop_table("jobs")
