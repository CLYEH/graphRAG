"""RB1-retry-core: builds.parent_build_id — retry lineage.

A retry (``POST /builds/{id}/retry``, DR-013) opens a NEW build that records
which build it retried, so the failed attempt's terminal record is never
mutated (audit integrity) and Console/observability can walk the lineage.

Deliberately NOT a foreign key to ``builds.id`` — mirroring
``pipeline_runs.build_id`` (see core.stores.tables): build retention/prune (C9)
isn't frozen, and every ondelete choice would pre-decide it (a SET NULL would
erase lineage on a parent prune; a RESTRICT would block pruning a retried
build). A plain nullable column keeps that decision open; NULL = not a retry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_builds_parent_build_id"
down_revision = "0018_pipeline_runs_by_build"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "builds",
        sa.Column("parent_build_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("builds", "parent_build_id")
