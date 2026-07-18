"""SRC2 (GAPS G2, DR-013): source soft-disable.

Sources gain an ``enabled`` flag (default true). A disabled source is excluded
from FUTURE ingests/builds; existing build_id-scoped projections (documents,
chunks, evidence) are never rewritten — corpus swap = disable old + register
new, historical provenance intact (option 2, the BA9 canonical-uri spirit).
NOT NULL with a server_default true so every pre-migration row is enabled — the
prior behavior — without a backfill pass.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_sources_enabled"
down_revision = "0016_entities_disambiguator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("sources", "enabled")
