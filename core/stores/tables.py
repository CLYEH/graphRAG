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
    # so reruns line up across runs. Non-empty (H6): an empty identifier is a
    # no-op identity — '' rows would collide under the dedup index and the
    # §27.7 retry set could never name the work they stand for.
    sa.Column("item_kind", sa.Text, nullable=False),
    sa.Column("item_ref", sa.Text, nullable=False),
    # No CHECK: 'failed'/'skipped' are the frozen *minimum* statuses (§4/§18
    # default verbosity), but sampled/all verbosity legitimately records
    # successes whose status name DESIGN doesn't freeze — a whitelist here
    # would make those rows unrepresentable.
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("message", sa.Text),
    sa.Column("error", postgresql.JSONB),
    sa.CheckConstraint("item_kind <> ''", name="pipeline_step_items_kind_nonempty"),
    sa.CheckConstraint("item_ref <> ''", name="pipeline_step_items_ref_nonempty"),
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
    # chunk refs inherit their source_uri from the document, and the frozen
    # MCP contract requires it non-empty (minLength 1) — a blank here would
    # strand every chunk under this document without a valid citation
    sa.CheckConstraint("source_uri <> ''", name="documents_source_uri_nonempty"),
    # content_hash is the frozen §18 stable item_ref key for documents
    # (core.observability.spec) — an empty identifier identifies nothing
    sa.CheckConstraint("content_hash <> ''", name="documents_content_hash_nonempty"),
    # FK target for the composite child FKs below (DR-006 build alignment)
    sa.UniqueConstraint("id", "build_id", name="documents_id_build_unique"),
)

documents_by_build = sa.Index("documents_by_build", documents.c.project, documents.c.build_id)

# DR-006: children reference their parent TOGETHER WITH build_id (composite
# FKs), so a child row provably lives in its parent's build — cross-build
# mixing (and cross-build cascade deletes) become unrepresentable instead of
# writer discipline. Where both sides carry project, it joins the FK too.
chunks = sa.Table(
    "chunks",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer),
    # NOT NULL + sane: the frozen MCP chunk-result contract requires every
    # chunk ref to carry non-negative offsets — a chunk stored without a
    # citable span could never be returned by C6. Chunking (C2) always knows
    # the span it cut.
    sa.Column("start_offset", sa.Integer, nullable=False),
    sa.Column("end_offset", sa.Integer, nullable=False),
    sa.Column("vector_point_id", postgresql.UUID(as_uuid=True)),
    sa.Column("metadata", postgresql.JSONB),
    sa.Column("status", sa.Text),
    sa.CheckConstraint(
        "start_offset >= 0 AND end_offset >= start_offset",
        name="chunks_span_sane",
    ),
    # ordinal is §4's chunk position within its document — a position index
    # has no negative interpretation
    sa.CheckConstraint("ordinal >= 0", name="chunks_ordinal_nonnegative"),
    sa.ForeignKeyConstraint(
        ["document_id", "build_id"],
        ["documents.id", "documents.build_id"],
        ondelete="CASCADE",
        name="chunks_document_build_fk",
    ),
    # position identity: one chunk per slot per document — a C2 retry/replay
    # writing a second row for the same position would make reconstruction
    # and indexing ambiguous (same invariant family as entity_key/evidence_hash)
    sa.UniqueConstraint("document_id", "ordinal", name="chunks_document_ordinal_unique"),
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
    # (non-empty by construction — fpv{N}: prefix; the CHECK pins it)
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
    sa.CheckConstraint("entity_key <> ''", name="entities_key_nonempty"),
    # FK target for relations/merge_candidates build+project alignment (DR-006)
    sa.UniqueConstraint("id", "project", "build_id", name="entities_id_project_build_unique"),
)

# §17/§27.3: entity_key IS the canonical identity — one canonical entity per
# key per build, as a UNIQUE index, or a single review_ledger decision would
# apply to several rows and projections would carry duplicate identities.
# C4 applies ledger decisions through this same path (§27.3 套用).
entities_by_key = sa.Index(
    "entities_by_key",
    entities.c.project,
    entities.c.build_id,
    entities.c.entity_key,
    unique=True,
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
    # §27.2: entity source_refs are mention-backed and the frozen SourceRef.id
    # has minLength 1 — a ref-less mention could never be cited.
    sa.Column("source_ref", sa.Text, nullable=False),
    sa.Column("surface_form", sa.Text),
    sa.Column("confidence", sa.REAL),
    sa.CheckConstraint(
        "source_kind IN ('structured','text')",
        name="entity_mentions_source_kind_valid",
    ),
    sa.CheckConstraint("source_ref <> ''", name="entity_mentions_source_ref_nonempty"),
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
    sa.Column("src_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("dst_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("attributes", postgresql.JSONB),
    # §27.3: fpv{N}(src_key|norm(type)|dst_key), minted AT EXTRACTION (C3) by
    # core.resolve.fingerprints — the per-build relation dedup rides the partial
    # unique index below, which excludes NULLs, so a NULL signature would let a
    # re-run duplicate the edge (breaks §5); extraction always sets it. C4
    # re-mints when a fuzzy merge changes an endpoint's entity_key.
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
    sa.CheckConstraint("relation_signature <> ''", name="relations_signature_nonempty"),
    # DR-006: endpoints must live in the same project AND build as the relation
    sa.ForeignKeyConstraint(
        ["src_entity_id", "project", "build_id"],
        ["entities.id", "entities.project", "entities.build_id"],
        ondelete="CASCADE",
        name="relations_src_entity_fk",
    ),
    sa.ForeignKeyConstraint(
        ["dst_entity_id", "project", "build_id"],
        ["entities.id", "entities.project", "entities.build_id"],
        ondelete="CASCADE",
        name="relations_dst_entity_fk",
    ),
    # FK target for relation_evidence build alignment
    sa.UniqueConstraint("id", "build_id", name="relations_id_build_unique"),
)

relations_by_src = sa.Index("relations_by_src", relations.c.src_entity_id)
relations_by_dst = sa.Index("relations_by_dst", relations.c.dst_entity_id)

# §17/§27.3: same identity invariant as entities_by_key, for the minted state —
# partial because pre-resolve rows legitimately carry no signature yet.
relations_by_signature = sa.Index(
    "relations_by_signature",
    relations.c.project,
    relations.c.build_id,
    relations.c.relation_signature,
    unique=True,
    postgresql_where=sa.text("relation_signature IS NOT NULL"),
)

relation_evidence = sa.Table(
    "relation_evidence",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_type", sa.Text, nullable=False),
    # NOT NULL for every type: the §27.4 hash formula
    # sha256(relation_signature | evidence_ref | norm(quote)) takes
    # evidence_ref as its source-distinguishing component — without it two
    # same-quote evidences from different sources collide under the dedup
    # index and one audit source is silently lost. (row: table+pk; chunk/
    # manual: the writer's source ref — encoding unfrozen, existence isn't.)
    sa.Column("evidence_ref", sa.Text, nullable=False),
    # Deliberately NOT an FK: §27.4 prune survival — evidence outlives the
    # chunk it quotes (quote/offsets/source_uri are denormalized below), so the
    # id must be allowed to dangle after the old chunk is pruned.
    sa.Column("chunk_id", postgresql.UUID(as_uuid=True)),
    sa.Column("start_offset", sa.Integer),
    sa.Column("end_offset", sa.Integer),
    # §27.4: keep the excerpt, not the whole chunk. DESIGN marks the 512 cap
    # 🔧 tunable, but the frozen P1/MCP schemas already pin maxLength 512 —
    # the contract governs (an overlong stored quote could only be emitted by
    # silently truncating the audit excerpt). Retuning means a contract
    # version bump anyway (DR-002), which is when this CHECK moves too.
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
    # §27.4 + the frozen MCP relation source-ref contract
    # (mcp_response.schema.json): chunk refs emit source_uri + quote + offsets,
    # document/manual refs emit source_uri + quote, row refs emit table+pk
    # (carried in evidence_ref). A row missing its type's provenance could
    # never produce a contract-valid, prune-surviving ref — reject at write.
    sa.CheckConstraint(
        "evidence_type <> 'chunk' OR "
        "(quote IS NOT NULL AND quote <> '' AND source_uri IS NOT NULL AND source_uri <> '')",
        name="relation_evidence_chunk_provenance",
    ),
    sa.CheckConstraint(
        "evidence_type <> 'manual' OR "
        "(quote IS NOT NULL AND quote <> '' AND source_uri IS NOT NULL AND source_uri <> '')",
        name="relation_evidence_manual_provenance",
    ),
    # subsumes the earlier row-only rule: every type's ref must exist (hash
    # identity input) and be non-empty (identifier rule)
    sa.CheckConstraint("evidence_ref <> ''", name="relation_evidence_ref_nonempty"),
    # frozen MCP contract: offsets are non-negative (minimum 0) and after
    # prune they are the only auditable span left — an inverted range could
    # never be a valid citation
    sa.CheckConstraint(
        "evidence_type <> 'chunk' OR (start_offset >= 0 AND end_offset >= start_offset)",
        name="relation_evidence_chunk_span_sane",
    ),
    sa.CheckConstraint(
        "quote IS NULL OR char_length(quote) <= 512",
        name="relation_evidence_quote_within_cap",
    ),
    # the §27.4 stable identity — a '' placeholder identifies nothing and
    # would make all later placeholder rows collide under the dedup index
    sa.CheckConstraint("evidence_hash <> ''", name="relation_evidence_hash_nonempty"),
    # DR-006: evidence lives in its relation's build
    sa.ForeignKeyConstraint(
        ["relation_id", "build_id"],
        ["relations.id", "relations.build_id"],
        ondelete="CASCADE",
        name="relation_evidence_relation_fk",
    ),
)

# FK support (like chunks_by_document etc.): the composite FK cascades and
# relation-detail lookups probe by (relation_id, build_id) — the dedup index
# below leads with build_id and cannot serve them
relation_evidence_by_relation = sa.Index(
    "relation_evidence_by_relation",
    relation_evidence.c.relation_id,
    relation_evidence.c.build_id,
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
    # §27.2: community_report results must cite member entity refs — a report
    # with no members (or NULL placeholders) has nothing to build the
    # contract-required refs from
    sa.Column(
        "member_entity_ids",
        postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
        nullable=False,
    ),
    sa.Column("rating", sa.REAL),
    sa.CheckConstraint(
        "cardinality(member_entity_ids) > 0 AND array_position(member_entity_ids, NULL) IS NULL",
        name="community_reports_members_citeable",
    ),
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
    sa.Column("left_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("right_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
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
    # a pair is two DISTINCT entities — sorted(k, k) merge identity is void
    sa.CheckConstraint(
        "left_entity_id <> right_entity_id",
        name="merge_candidates_distinct_pair",
    ),
    # DR-006: both candidates live in the same project AND build as the pair
    sa.ForeignKeyConstraint(
        ["left_entity_id", "project", "build_id"],
        ["entities.id", "entities.project", "entities.build_id"],
        ondelete="CASCADE",
        name="merge_candidates_left_entity_fk",
    ),
    sa.ForeignKeyConstraint(
        ["right_entity_id", "project", "build_id"],
        ["entities.id", "entities.project", "entities.build_id"],
        ondelete="CASCADE",
        name="merge_candidates_right_entity_fk",
    ),
)

merge_candidates_by_build = sa.Index(
    "merge_candidates_by_build",
    merge_candidates.c.project,
    merge_candidates.c.build_id,
    merge_candidates.c.status,
)

# FK support — both existing merge_candidates indexes lead with project and
# cannot serve entity-id cascade/lookup probes
merge_candidates_by_left = sa.Index(
    "merge_candidates_by_left",
    merge_candidates.c.left_entity_id,
    merge_candidates.c.project,
    merge_candidates.c.build_id,
)
merge_candidates_by_right = sa.Index(
    "merge_candidates_by_right",
    merge_candidates.c.right_entity_id,
    merge_candidates.c.project,
    merge_candidates.c.build_id,
)

# §17/§27.3: merge review identity is the SYMMETRIC pair — merge_key =
# fpv(sorted(left_key, right_key)). Within a build, entity id ↔ entity_key is
# 1:1 (entities_id_project_build_unique + entities_by_key), so LEAST/GREATEST
# over the ids enforces the same sorted-pair identity: (A,B) and (B,A) are one
# candidate, and a duplicate would leave a decided pair coexisting with a
# still-pending twin.
merge_candidates_pair_unique = sa.Index(
    "merge_candidates_pair_unique",
    merge_candidates.c.project,
    merge_candidates.c.build_id,
    sa.text("LEAST(left_entity_id, right_entity_id)"),
    sa.text("GREATEST(left_entity_id, right_entity_id)"),
    unique=True,
)

# §6 待審池: LLM-proposed ontology types awaiting Console review (C3c).
# Deliberately NOT build-scoped — like review_ledger, this is a REVIEW
# artifact keyed by a stable fingerprint (proposal_key, DR-007 versioned), so
# carry-forward is structural: a later build re-proposing the same type
# upserts into the existing row and a rejected type never re-opens review.
# §17 state machine: proposed → accepted|rejected (core.resolve.review).
ontology_proposals = sa.Table(
    "ontology_proposals",
    metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),  # what the type would type
    sa.Column("type_name", sa.Text, nullable=False),  # as first observed
    sa.Column("proposal_key", sa.Text, nullable=False),  # fpv(norm(kind)|norm(type_name))
    sa.Column("fingerprint_version", sa.Integer, nullable=False),
    sa.Column("example", sa.Text),  # first observed name/quote
    sa.Column("chunk_ref", sa.Text),  # first observed source (content-stable string)
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'proposed'")),
    sa.Column("decided_by", sa.Text),
    sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("reason", sa.Text),
    sa.Column(
        "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.CheckConstraint("project <> ''", name="ontology_proposals_project_nonempty"),
    sa.CheckConstraint("kind IN ('entity','relation')", name="ontology_proposals_kind_valid"),
    sa.CheckConstraint("type_name <> ''", name="ontology_proposals_type_nonempty"),
    sa.CheckConstraint("proposal_key <> ''", name="ontology_proposals_key_nonempty"),
    sa.CheckConstraint(
        "status IN ('proposed','accepted','rejected')", name="ontology_proposals_status_valid"
    ),
    # §17 conditional pair, both directions: a decided row must say who/when;
    # an undecided row must not carry decision residue.
    sa.CheckConstraint(
        "(status = 'proposed' AND decided_by IS NULL AND decided_at IS NULL) "
        "OR (status <> 'proposed' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="ontology_proposals_decision_fields_iff_decided",
    ),
)

# The stable identity: one pool row per proposed type per project.
ontology_proposals_by_key = sa.Index(
    "ontology_proposals_by_key",
    ontology_proposals.c.project,
    ontology_proposals.c.proposal_key,
    unique=True,
)
