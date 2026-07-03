"""P2: builds table + one_active_build partial unique index.

DESIGN §14/§27.1, DR-001/DR-006: every core object is versioned by build;
"at most one active build per project" is enforced HERE, in the database,
by a partial unique index — the application never gets to be wrong about it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_builds"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "builds",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'building'")),
        sa.Column("config_hash", sa.Text),
        sa.Column("source_hash", sa.Text),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("metrics", postgresql.JSONB),
        sa.Column("eval", postgresql.JSONB),
        sa.CheckConstraint(
            "status IN ('building','ready','active','failed','archived')",
            name="builds_status_valid",
        ),
    )
    op.create_index(
        "one_active_build",
        "builds",
        ["project"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("one_active_build", table_name="builds")
    op.drop_table("builds")
