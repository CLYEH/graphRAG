"""H6: pipeline_step_items identifier non-empty CHECKs.

The identifier rule (empty string identifies nothing) applied to the P6 table
that predates it: '' item_kind/item_ref would be a no-op identity — rows would
collide under pipeline_step_items_dedup and the §27.7 retry-failed-only set
could never name the work such rows stand for. PR #17's retro filed this after
the same rule landed for every C1a identifier.
"""

from __future__ import annotations

from alembic import op

revision = "0005_item_identifiers_nonempty"
down_revision = "0004_core_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "pipeline_step_items_kind_nonempty", "pipeline_step_items", "item_kind <> ''"
    )
    op.create_check_constraint(
        "pipeline_step_items_ref_nonempty", "pipeline_step_items", "item_ref <> ''"
    )


def downgrade() -> None:
    op.drop_constraint("pipeline_step_items_ref_nonempty", "pipeline_step_items")
    op.drop_constraint("pipeline_step_items_kind_nonempty", "pipeline_step_items")
