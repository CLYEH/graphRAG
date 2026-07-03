"""P3: review_ledger — cross-build review carry-forward.

DESIGN §17/§27.3, DR-003/DR-007: decisions live outside build scope, keyed by
stable fingerprint (target_key) + the fingerprint_version they were minted
under. resolve/index apply them on every build: reject -> excluded from
projections, approve/merge -> adopted, defer -> still reviewable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_review_ledger"
down_revision = "0001_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("target_kind", sa.Text, nullable=False),
        sa.Column("target_key", sa.Text, nullable=False),
        sa.Column("fingerprint_version", sa.Integer, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("decided_by", sa.Text, nullable=False),
        sa.Column(
            "decided_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reason", sa.Text),
        sa.CheckConstraint(
            "target_kind IN ('entity','relation','merge')",
            name="review_ledger_kind_valid",
        ),
        sa.CheckConstraint(
            "decision IN ('approve','reject','defer','merge','split')",
            name="review_ledger_decision_valid",
        ),
    )
    op.create_index(
        "review_ledger_lookup",
        "review_ledger",
        ["project", "target_kind", "target_key", "fingerprint_version"],
    )


def downgrade() -> None:
    op.drop_index("review_ledger_lookup", table_name="review_ledger")
    op.drop_table("review_ledger")
