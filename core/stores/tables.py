"""Postgres source-of-record table definitions (DESIGN §4), SQLAlchemy Core.

Alembic migrations render these into DDL (`migrations/versions/`), and the
build-scoped repository layer (C1) queries through them. P2 freezes the
build/activation model: the `builds` table plus the `one_active_build`
partial unique index that makes "at most one active build per project" a
database invariant rather than an application promise (DR-001/DR-006, §27.1).
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
