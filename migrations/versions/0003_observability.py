"""P6: pipeline_runs/steps/items — three-layer observability.

DESIGN §18/§27.7: one run per pipeline execution, one row per step, item rows
per work item (default verbosity: failed/skipped only). item_ref is a stable
key (document=content_hash, entity=entity_key — core.observability.spec), so
reruns line up; the per-step unique index makes item dedup a DB invariant.
§27.7 build binding: ingest runs must carry the building build's id; only
pure source-validation jobs may leave build_id null.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_observability"
down_revision = "0002_review_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project", sa.Text, nullable=False),
        # nullable on purpose (§27.7); no FK to builds until C9 freezes prune
        sa.Column("build_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("config_hash", sa.Text),
        sa.Column("source_hash", sa.Text),
        sa.Column("created_by", sa.Text),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("metrics", postgresql.JSONB),
        sa.Column("error", postgresql.JSONB),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled')",
            name="pipeline_runs_status_valid",
        ),
        sa.CheckConstraint(
            "kind <> 'ingest' OR build_id IS NOT NULL",
            name="pipeline_runs_ingest_has_build",
        ),
    )
    op.create_table(
        "pipeline_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("input_count", sa.Integer),
        sa.Column("output_count", sa.Integer),
        sa.Column("skipped_count", sa.Integer),
        sa.Column("failed_count", sa.Integer),
        sa.Column("metrics", postgresql.JSONB),
        sa.Column("error", postgresql.JSONB),
    )
    op.create_index("pipeline_steps_by_run", "pipeline_steps", ["run_id"])
    op.create_table(
        "pipeline_step_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_kind", sa.Text, nullable=False),
        sa.Column("item_ref", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("message", sa.Text),
        sa.Column("error", postgresql.JSONB),
    )
    op.create_index(
        "pipeline_step_items_dedup",
        "pipeline_step_items",
        ["step_id", "item_kind", "item_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("pipeline_step_items_dedup", table_name="pipeline_step_items")
    op.drop_table("pipeline_step_items")
    op.drop_index("pipeline_steps_by_run", table_name="pipeline_steps")
    op.drop_table("pipeline_steps")
    op.drop_table("pipeline_runs")
