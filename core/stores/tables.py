"""Postgres source-of-record table definitions (DESIGN §4), SQLAlchemy Core.

Alembic migrations render these into DDL (`migrations/versions/`), and the
build-scoped repository layer (C1) queries through them. P2 freezes the
build/activation model: the `builds` table plus the `one_active_build`
partial unique index that makes "at most one active build per project" a
database invariant rather than an application promise (DR-001/DR-006, §27.1).
P6 freezes the three-layer observability schema (§18/§27.7) — see
`core.observability.spec` for the item_ref rules the tables encode.
C1a adds the remaining §4 core tables (documents → community_reports +
merge_candidates), all build-scoped per DR-006.

Constraint policy (per-column): NOT NULL is a superset of the frozen
contract's required lists (every contract-required field is NOT NULL, plus
scoping/lifecycle-init columns like project and the review_status default);
CHECK constraints exist exactly where §4's comments or the frozen contract
enums name the vocabulary (LifecycleStatus / ReviewStatus / CreatedBy /
MergeCandidateStatus / evidence_type / decision) — contract tests pin the
lockstep. Columns the contract leaves as free strings (documents.status,
chunks.status) get no CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

# §4/§17 + contract LifecycleStatus/ReviewStatus/CreatedBy — frozen enums
# shared by entities and relations (contract tests pin them to openapi.yaml).
LIFECYCLE_STATUSES = ("active", "deprecated", "merged", "rejected", "needs_review")
REVIEW_STATUSES = ("unreviewed", "approved", "rejected")
CREATED_BY = ("rule", "llm", "manual")
EVIDENCE_TYPES = ("chunk", "row", "manual")
MERGE_CANDIDATE_STATUSES = ("pending", "approved", "rejected", "deferred")
MERGE_CANDIDATE_DECISIONS = ("approve", "reject", "defer")

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

# --- C1a: §4 core tables (all build-scoped, DR-006) ---------------------------

documents = sa.Table(
    "documents",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_uri", sa.Text, nullable=False),
    sa.Column("raw", sa.Text),
    # §5: content_hash drives skip/rerun decisions and is the stable item_ref
    # for document items (§18 / core.observability.spec)
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("mime", sa.Text),
    sa.Column("metadata", postgresql.JSONB),
    sa.Column("status", sa.Text),
    sa.Column("ingested_at", sa.TIMESTAMP(timezone=True)),
)

documents_by_build = sa.Index("documents_by_build", documents.c.project, documents.c.build_id)

chunks = sa.Table(
    "chunks",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column(
        "document_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("start_offset", sa.Integer),
    sa.Column("end_offset", sa.Integer),
    sa.Column("vector_point_id", postgresql.UUID(as_uuid=True)),
    sa.Column("metadata", postgresql.JSONB),
    sa.Column("status", sa.Text),
)

chunks_by_document = sa.Index("chunks_by_document", chunks.c.document_id)

entities = sa.Table(
    "entities",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("canonical_name", sa.Text, nullable=False),
    # §27.3: cross-build stable identity, minted by core.resolve.fingerprints
    sa.Column("entity_key", sa.Text, nullable=False),
    sa.Column("attributes", postgresql.JSONB),
    sa.Column("embedding_point_id", postgresql.UUID(as_uuid=True)),
    sa.Column("status", sa.Text, nullable=False),
    # §17: review lifecycle starts unreviewed (core.resolve.review state machine)
    sa.Column("review_status", sa.Text, nullable=False, server_default=sa.text("'unreviewed'")),
    sa.Column("created_by", sa.Text),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True)),
    sa.CheckConstraint(
        "status IN ('active','deprecated','merged','rejected','needs_review')",
        name="entities_status_valid",
    ),
    sa.CheckConstraint(
        "review_status IN ('unreviewed','approved','rejected')",
        name="entities_review_status_valid",
    ),
    sa.CheckConstraint(
        "created_by IN ('rule','llm','manual')",
        name="entities_created_by_valid",
    ),
)

# C4 applies review_ledger decisions by stable key within the building build (§27.3 套用)
entities_by_key = sa.Index(
    "entities_by_key", entities.c.project, entities.c.build_id, entities.c.entity_key
)

entity_mentions = sa.Table(
    "entity_mentions",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column(
        "entity_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("source_ref", sa.Text),
    sa.Column("surface_form", sa.Text),
    sa.Column("confidence", sa.REAL),
    sa.CheckConstraint(
        "source_kind IN ('structured','text')",
        name="entity_mentions_source_kind_valid",
    ),
)

entity_mentions_by_entity = sa.Index("entity_mentions_by_entity", entity_mentions.c.entity_id)

relations = sa.Table(
    "relations",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "src_entity_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "dst_entity_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("attributes", postgresql.JSONB),
    # §27.3: fpv{N}(src_key|norm(type)|dst_key), minted by core.resolve.fingerprints
    sa.Column("relation_signature", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("review_status", sa.Text, nullable=False, server_default=sa.text("'unreviewed'")),
    sa.Column("created_by", sa.Text),
    sa.Column("confidence", sa.REAL),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True)),
    sa.CheckConstraint(
        "status IN ('active','deprecated','merged','rejected','needs_review')",
        name="relations_status_valid",
    ),
    sa.CheckConstraint(
        "review_status IN ('unreviewed','approved','rejected')",
        name="relations_review_status_valid",
    ),
    sa.CheckConstraint(
        "created_by IN ('rule','llm','manual')",
        name="relations_created_by_valid",
    ),
)

relations_by_src = sa.Index("relations_by_src", relations.c.src_entity_id)
relations_by_dst = sa.Index("relations_by_dst", relations.c.dst_entity_id)

relation_evidence = sa.Table(
    "relation_evidence",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column(
        "relation_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("relations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_type", sa.Text, nullable=False),
    sa.Column("evidence_ref", sa.Text),
    # Deliberately NOT an FK: §27.4 prune survival — evidence outlives the
    # chunk it quotes (quote/offsets/source_uri are denormalized below), so the
    # id must be allowed to dangle after the old chunk is pruned.
    sa.Column("chunk_id", postgresql.UUID(as_uuid=True)),
    sa.Column("start_offset", sa.Integer),
    sa.Column("end_offset", sa.Integer),
    # §27.4: keep the excerpt, not the whole chunk (length cap 🔧 512 — tunable,
    # so no DB CHECK)
    sa.Column("quote", sa.Text),
    # §27.4 + P1 contract: denormalized provenance so evidence survives pruning.
    # §4's terse column list omits it; §27.4 and the frozen RelationEvidence
    # contract field are explicit, so the table follows them (DESIGN §4 synced
    # in this change).
    sa.Column("source_uri", sa.Text),
    # §27.4: sha256(relation_signature | evidence_ref | norm(quote)) — dedup +
    # stable identity. NOT NULL: the formula is computable for every row, and
    # a NULL would vacuously escape the dedup index below (Postgres treats
    # NULLs as distinct), quietly voiding the §27.4 invariant.
    sa.Column("evidence_hash", sa.Text, nullable=False),
    sa.Column("confidence", sa.REAL),
    sa.Column(
        "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.CheckConstraint(
        "evidence_type IN ('chunk','row','manual')",
        name="relation_evidence_type_valid",
    ),
    # §27.4 offsets semantics by evidence_type: chunk evidence MUST carry its
    # extraction span; manual evidence is DELIBERATELY span-less (document-level
    # citation keeps quote + source_uri only). row evidence has no frozen
    # offsets rule.
    sa.CheckConstraint(
        "evidence_type <> 'chunk' OR (start_offset IS NOT NULL AND end_offset IS NOT NULL)",
        name="relation_evidence_chunk_has_span",
    ),
    sa.CheckConstraint(
        "evidence_type <> 'manual' OR (start_offset IS NULL AND end_offset IS NULL)",
        name="relation_evidence_manual_spanless",
    ),
)

# §27.4 dedup: the hash already embeds relation_signature, so per-build
# uniqueness = one row per distinct evidence
relation_evidence_dedup = sa.Index(
    "relation_evidence_dedup",
    relation_evidence.c.build_id,
    relation_evidence.c.evidence_hash,
    unique=True,
)

community_reports = sa.Table(
    "community_reports",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("level", sa.Integer, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("summary", sa.Text),
    sa.Column("member_entity_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
    sa.Column("rating", sa.REAL),
)

community_reports_by_build = sa.Index(
    "community_reports_by_build", community_reports.c.project, community_reports.c.build_id
)

merge_candidates = sa.Table(
    "merge_candidates",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "left_entity_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "right_entity_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("score", sa.REAL, nullable=False),
    sa.Column("features", postgresql.JSONB),
    # §17: pending → approved|rejected|deferred (core.resolve.review state machine)
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    sa.Column("decision", sa.Text),
    sa.Column("decided_by", sa.Text),
    sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("reason", sa.Text),
    # §17: impact preview + snapshots make review decisions auditable/undoable
    sa.Column("impact", postgresql.JSONB),
    sa.Column("left_snapshot", postgresql.JSONB),
    sa.Column("right_snapshot", postgresql.JSONB),
    sa.CheckConstraint(
        "status IN ('pending','approved','rejected','deferred')",
        name="merge_candidates_status_valid",
    ),
    # P0 contract MergeCandidate.decision enum: approve|reject|defer
    sa.CheckConstraint(
        "decision IN ('approve','reject','defer')",
        name="merge_candidates_decision_valid",
    ),
)

merge_candidates_by_build = sa.Index(
    "merge_candidates_by_build",
    merge_candidates.c.project,
    merge_candidates.c.build_id,
    merge_candidates.c.status,
)
