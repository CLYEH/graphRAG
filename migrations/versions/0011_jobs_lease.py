"""BA2d-1: jobs execution lease — lease_owner + lease_expires_at.

run_build's FOR UPDATE lock (BA2c-1) serializes build *creation* but releases at
the build-resolution commit, so two dispatches of one job then execute all six
stages concurrently against the same building build — safe under convergent
idempotency, but double the LLM cost and racing derived-store writes. These two
nullable columns back the execution lease (``core.builds.lease``): a worker
claims the lease with an atomic conditional UPDATE, heartbeats ``lease_expires_at``
while run_build runs, and clears both on exit. A crashed holder stops
heartbeating and its lease expires on the DB clock, so the next dispatch reclaims
it — the lease distinguishes "actively running" from "crashed running" without
stranding a job on a dead worker.

Both nullable and paired (a ``(owner IS NULL) = (expires IS NULL)`` CHECK): a job
with no in-flight execution is simply unleased. No backfill — every existing job
is already unleased (both NULL), which satisfies the CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_jobs_lease"
down_revision = "0010_builds_project_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True))
    # acquire sets both, release clears both — a half-set lease can never exist to
    # confuse an expiry check (a NULL expiry would read as "never expires").
    op.create_check_constraint(
        "jobs_lease_paired", "jobs", "(lease_owner IS NULL) = (lease_expires_at IS NULL)"
    )
    # a lease owner is a worker id; an empty one would collapse the owner-guard
    # (two empty-owner workers could renew/release each other's lease). Same
    # non-empty-identifier rule as jobs_kind_nonempty.
    op.create_check_constraint(
        "jobs_lease_owner_nonempty", "jobs", "lease_owner IS NULL OR lease_owner <> ''"
    )


def downgrade() -> None:
    op.drop_constraint("jobs_lease_owner_nonempty", "jobs", type_="check")
    op.drop_constraint("jobs_lease_paired", "jobs", type_="check")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "lease_owner")
