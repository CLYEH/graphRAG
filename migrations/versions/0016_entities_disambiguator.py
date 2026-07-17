"""GOV1/DR-011: persist the entity disambiguator on the row.

The disambiguator (a stable external id, §27.3) was only ever baked into the
entity_key HASH — its value was unrecoverable from stored rows. The type-free
v2 review-ledger keys (DR-011) re-mint from ``(canonical_name, disambiguator)``
at resolve/decision time, so the value must live on the row. Nullable + no
backfill: text-extraction entities never carry one, and pre-migration rows
belong to already-built builds — resolve only ever runs over rows its own
build minted, which post-deploy code always stamps. (The one legacy edge — a
pre-deploy build RETRIED across the deploy — mints its ledger keys without the
disambiguator; those keys govern only that build's carry-forward and are
superseded the next full build.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_entities_disambiguator"
down_revision = "0015_jobs_eval_inputs_fp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("entities", sa.Column("disambiguator", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("entities", "disambiguator")
