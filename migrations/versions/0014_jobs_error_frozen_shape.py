"""BA2e-1: full frozen Error shape on jobs.error + queued-sweep index.

1. Backfill: error rows written before BA2e-1 carry only {code, message,
   details} — the frozen Error also requires request_id, and GET /jobs/{id}
   passes jobs.error through verbatim, so an upgraded database would keep
   serving contract-invalid Job.error to generated clients. Every legacy
   error is normalized to the full four-key shape (a fresh uuid per row,
   the same failure-record semantics the writers now mint; any other
   missing key is coalesced to its neutral value so the CHECK below holds
   over ALL history, including hand-written test rows).
2. jobs_error_frozen_shape CHECK: the full shape becomes a storage
   invariant instead of writer discipline — a future partial writer fails
   loudly at the INSERT/UPDATE rather than leaking an invalid shape to the
   API (the class-10 lesson: the decisive check lives in the write).
3. jobs_unenqueued: partial index for the BA2e queued-sweep
   (find_unenqueued_jobs — queued, never leased, older than the grace),
   the exact sibling of 0013's jobs_reapable: the covered set is
   near-empty in a healthy system (only jobs awaiting their first
   dispatch), so each 30s reaper tick is an index probe rather than a seq
   scan over the whole job history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_jobs_error_frozen_shape"
down_revision = "0013_jobs_reaper_index"
branch_labels = None
depends_on = None

_REQUIRED_KEYS = "ARRAY['code','message','details','request_id']"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE jobs
        SET error = jsonb_build_object(
                'code',       COALESCE(error->'code', '"INTERNAL"'::jsonb),
                'message',    COALESCE(error->'message', '""'::jsonb),
                'details',    COALESCE(error->'details', 'null'::jsonb),
                'request_id', COALESCE(error->'request_id',
                                       to_jsonb(gen_random_uuid()::text))
            )
        WHERE error IS NOT NULL
          AND NOT (jsonb_typeof(error) = 'object' AND error ?& {_REQUIRED_KEYS})
        """
    )
    # jsonb_typeof guards the degenerate non-object cases ?& alone would admit
    # (an array of those four strings) — the invariant is fully self-standing,
    # not reliant on the writers' dict typing
    op.create_check_constraint(
        "jobs_error_frozen_shape",
        "jobs",
        sa.text(f"error IS NULL OR (jsonb_typeof(error) = 'object' AND error ?& {_REQUIRED_KEYS})"),
    )
    op.create_index(
        "jobs_unenqueued",
        "jobs",
        ["created_at"],
        postgresql_where=sa.text("lease_owner IS NULL AND status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("jobs_unenqueued", table_name="jobs")
    op.drop_constraint("jobs_error_frozen_shape", "jobs", type_="check")
    # the backfill is not reversed: the stamped request_ids are valid under the
    # pre-0014 (unconstrained) schema too, and dropping them would re-create
    # exactly the contract-invalid rows this migration exists to eliminate
