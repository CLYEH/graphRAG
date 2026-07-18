"""RB1: index pipeline_runs by (project, build_id).

The RB1 step/item drill-down reads (and, later, retry-failed-only's run merge)
scope by the run's ``(project, build_id)``. Without this index each request-path
list/precheck scans every historical run before it can use the
``pipeline_steps.run_id`` index — add the supporting index.
"""

from __future__ import annotations

from alembic import op

revision = "0018_pipeline_runs_by_build"
down_revision = "0017_sources_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("pipeline_runs_by_build", "pipeline_runs", ["project", "build_id"])


def downgrade() -> None:
    op.drop_index("pipeline_runs_by_build", table_name="pipeline_runs")
