"""Postgres source-of-record table definitions (DESIGN §4), SQLAlchemy Core.

Alembic migrations render these into DDL (`migrations/versions/`), and the
build-scoped repository layer (C1) queries through them. P2 freezes the
build/activation model: the `builds` table plus the `one_active_build`
partial unique index that makes "at most one active build per project" a
database invariant rather than an application promise (DR-001/DR-006, §27.1).
P6 freezes the three-layer observability schema (§18/§27.7) — see
`core.observability.spec` for the item_ref rules the tables encode.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

# DESIGN §4: builds.status lifecycle — frozen enum, enforced by CHECK constraint.
BUILD_STATUSES = ("building", "ready", "active", "failed", "archived")

builds = sa.Table(
    "builds",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'building'")),
    sa.Column("config_hash", sa.Text),
    sa.Column("source_hash", sa.Text),
    sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("activated_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("metrics", postgresql.JSONB),
    sa.Column("eval", postgresql.JSONB),
    sa.CheckConstraint(
        "status IN ('building','ready','active','failed','archived')",
        name="builds_status_valid",
    ),
)

# §27.1 / DR-001: `CREATE UNIQUE INDEX one_active_build ON builds(project)
# WHERE status='active'` — the single source of truth for the active build.
one_active_build = sa.Index(
    "one_active_build",
    builds.c.project,
    unique=True,
    postgresql_where=sa.text("status = 'active'"),
)

# §17 / DR-003: review decisions are deliberately NOT build-scoped — they carry
# forward across rebuilds, keyed by stable fingerprints (core.resolve.fingerprints)
# plus the fingerprint_version they were minted under (§27.3 / DR-007).
review_ledger = sa.Table(
    "review_ledger",
    metadata,
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
        "decided_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
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

# resolve-time application scans all entries for a key (precedence is computed
# in core.resolve.review.effective_decision, not in SQL)
review_ledger_lookup = sa.Index(
    "review_ledger_lookup",
    review_ledger.c.project,
    review_ledger.c.target_kind,
    review_ledger.c.target_key,
    review_ledger.c.fingerprint_version,
)

# §18: three-layer observability — one run per pipeline execution, one row per
# step, item rows per work item (default verbosity records only failed/skipped
# items; 🔧 observability.item_logging).
pipeline_runs = sa.Table(
    "pipeline_runs",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    # §27.7: nullable on purpose — pure source-validation jobs carry no build.
    # Deliberately NOT an FK to builds: build retention/prune (C9) isn't frozen
    # yet and every ondelete choice would pre-decide it (SET NULL would even
    # break the build-binding CHECK below). Revisit with C9.
    sa.Column("build_id", postgresql.UUID(as_uuid=True)),
    # Open vocabulary otherwise — the frozen §15 contract keeps Job.kind a
    # free string ("e.g. ingest, build, reproject"); only the build binding
    # below constrains it.
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
    sa.Column("config_hash", sa.Text),
    sa.Column("source_hash", sa.Text),
    sa.Column("created_by", sa.Text),
    sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("metrics", postgresql.JSONB),
    sa.Column("error", postgresql.JSONB),
    # Runs surface through the jobs API (§15/BA2), so status is the frozen
    # JobStatus enum (§27.2) — a fork here would hand the API rows it cannot
    # serialize (a contract test pins the lockstep).
    sa.CheckConstraint(
        "status IN ('queued','running','done','failed','cancelled')",
        name="pipeline_runs_status_valid",
    ),
    # §27.7: only the pure source-validation job is build-unbound — every
    # other kind (ingest, build, reproject, …) must carry the building
    # build's id, or the row could never be tied back to a build for §18
    # observability or retry-failed-only merging. Single-sourced as
    # core.observability.spec.SOURCE_VALIDATION_RUN_KIND (lockstep-tested);
    # a future build-unbound kind extends this by migration.
    sa.CheckConstraint(
        "build_id IS NOT NULL OR kind = 'source_validation'",
        name="pipeline_runs_build_binding",
    ),
)

pipeline_steps = sa.Table(
    "pipeline_steps",
    metadata,
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
    # No CHECK: §4/§18 freeze no step-status enum. Pinning one now would either
    # invent names or make DESIGN-legitimate rows unrepresentable; the C2–C7
    # writers firm this up.
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

pipeline_steps_by_run = sa.Index("pipeline_steps_by_run", pipeline_steps.c.run_id)

pipeline_step_items = sa.Table(
    "pipeline_step_items",
    metadata,
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
    # §18: item_ref is a *stable key* per item_kind (document=content_hash,
    # entity=entity_key — core.observability.spec.ITEM_REF_STABLE_KEYS_MIN),
    # so reruns line up across runs.
    sa.Column("item_kind", sa.Text, nullable=False),
    sa.Column("item_ref", sa.Text, nullable=False),
    # No CHECK: 'failed'/'skipped' are the frozen *minimum* statuses (§4/§18
    # default verbosity), but sampled/all verbosity legitimately records
    # successes whose status name DESIGN doesn't freeze — a whitelist here
    # would make those rows unrepresentable.
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("message", sa.Text),
    sa.Column("error", postgresql.JSONB),
)

# §27.7: retry-failed-only is idempotent by item_ref dedup — within one step
# execution an item has exactly one outcome row, as a database invariant.
# (Cross-run dedup — the retry input set — is computed in
# core.observability.spec.retry_failed_only, not in SQL: retried items land in
# NEW step rows of the same build.)
pipeline_step_items_dedup = sa.Index(
    "pipeline_step_items_dedup",
    pipeline_step_items.c.step_id,
    pipeline_step_items.c.item_kind,
    pipeline_step_items.c.item_ref,
    unique=True,
)
