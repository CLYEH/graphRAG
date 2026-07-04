"""C3c: ontology_proposals — the §6 待審池 for LLM-proposed types.

NOT build-scoped (like review_ledger): a review artifact keyed by the stable
DR-007-versioned proposal_key, so carry-forward is structural — a later build
re-proposing the same type upserts into the existing row, and a rejected type
never re-opens review. §17 states: proposed → accepted|rejected; the decision
fields are present IFF decided (both directions CHECKed).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_ontology_proposals"
down_revision = "0005_item_identifiers_nonempty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ontology_proposals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("type_name", sa.Text, nullable=False),
        sa.Column("proposal_key", sa.Text, nullable=False),
        sa.Column("fingerprint_version", sa.Integer, nullable=False),
        sa.Column("example", sa.Text),
        sa.Column("chunk_ref", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("decided_by", sa.Text),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("reason", sa.Text),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("project <> ''", name="ontology_proposals_project_nonempty"),
        sa.CheckConstraint("kind IN ('entity','relation')", name="ontology_proposals_kind_valid"),
        sa.CheckConstraint("type_name <> ''", name="ontology_proposals_type_nonempty"),
        sa.CheckConstraint("proposal_key <> ''", name="ontology_proposals_key_nonempty"),
        sa.CheckConstraint(
            "status IN ('proposed','accepted','rejected')",
            name="ontology_proposals_status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND decided_by IS NULL AND decided_at IS NULL) "
            "OR (status <> 'proposed' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ontology_proposals_decision_fields_iff_decided",
        ),
    )
    op.create_index(
        "ontology_proposals_by_key",
        "ontology_proposals",
        ["project", "proposal_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ontology_proposals_by_key", table_name="ontology_proposals")
    op.drop_table("ontology_proposals")
