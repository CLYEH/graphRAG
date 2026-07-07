"""BA1b: idempotency_keys — the §27 Idempotency-Key store.

Backs the reserve-first idempotency on write endpoints: `key` is the PK, which
both stores one response per key and serializes concurrent same-key requests.
`response`/`status` are nullable (a request reserves its key before running the
handler, fills them on success); `status` is the HTTP status to replay;
`expires_at` = created_at + the tunable TTL. New table, no backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_idempotency_keys"
down_revision = "0007_projects_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("response", postgresql.JSONB),
        sa.Column("status", sa.Integer),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("key <> ''", name="idempotency_keys_key_nonempty"),
        sa.CheckConstraint("request_hash <> ''", name="idempotency_keys_request_hash_nonempty"),
        # reserved (both null) or committed (both set) — never half-filled
        sa.CheckConstraint(
            "(status IS NULL) = (response IS NULL)", name="idempotency_keys_reserve_or_filled"
        ),
    )
    op.create_index("idempotency_keys_expiry", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idempotency_keys_expiry", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
