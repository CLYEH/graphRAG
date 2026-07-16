"""UXC1b triage 27: pin the accepted eval-inputs fingerprint on the job.

The async eval endpoint fingerprints the golden set + query policy at ACCEPT time
(folded into the Idempotency-Key request hash), but the worker reads those files at
DISPATCH time. If a user edits them between the 202 and dispatch, the job would score
DIFFERENT bytes than were accepted, while the idempotency key stays scoped to the old
fingerprint (retries conflict, or a run replays inputs the client never accepted). This
nullable column pins the accept-time fingerprint so the worker can re-fingerprint the
live inputs and fail loud on drift — the job evaluates the ACCEPTED inputs or nothing.
Nullable + no backfill: only eval jobs carry it, and a pre-existing eval job with no pin
simply skips the check (the worker guards on NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_jobs_eval_inputs_fp"
down_revision = "0014_jobs_error_frozen_shape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("eval_inputs_fingerprint", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "eval_inputs_fingerprint")
