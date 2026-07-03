"""C1a: §4 core tables — documents/chunks/entities/mentions/relations/evidence/reports/candidates.

All build-scoped (DR-006). Enum CHECKs mirror the frozen contract vocabularies
(LifecycleStatus/ReviewStatus/CreatedBy/MergeCandidateStatus/evidence_type);
§27.4's offsets-by-evidence_type semantics and evidence dedup are database
invariants; relation_evidence.chunk_id has NO FK so evidence survives chunk
pruning (§27.4, denormalized quote/offsets/source_uri).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_core_tables"
down_revision = "0003_observability"
branch_labels = None
depends_on = None


def _uuid_pk() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    op.create_table(
        "documents",
        _uuid_pk(),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_uri", sa.Text, nullable=False),
        sa.Column("raw", sa.Text),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("mime", sa.Text),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("status", sa.Text),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("id", "build_id", name="documents_id_build_unique"),
    )
    op.create_index("documents_by_build", "documents", ["project", "build_id"])

    # DR-006: composite child FKs — a child row provably lives in its parent's
    # build (and project where both sides carry it); cross-build mixing is
    # unrepresentable, not writer discipline
    op.create_table(
        "chunks",
        _uuid_pk(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer),
        sa.Column("start_offset", sa.Integer),
        sa.Column("end_offset", sa.Integer),
        sa.Column("vector_point_id", postgresql.UUID(as_uuid=True)),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("status", sa.Text),
        sa.ForeignKeyConstraint(
            ["document_id", "build_id"],
            ["documents.id", "documents.build_id"],
            ondelete="CASCADE",
            name="chunks_document_build_fk",
        ),
    )
    op.create_index("chunks_by_document", "chunks", ["document_id"])

    op.create_table(
        "entities",
        _uuid_pk(),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("canonical_name", sa.Text, nullable=False),
        sa.Column("entity_key", sa.Text, nullable=False),
        sa.Column("attributes", postgresql.JSONB),
        sa.Column("embedding_point_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.Text, nullable=False),
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
        sa.UniqueConstraint("id", "project", "build_id", name="entities_id_project_build_unique"),
    )
    # §17/§27.3: one canonical entity per key per build
    op.create_index(
        "entities_by_key", "entities", ["project", "build_id", "entity_key"], unique=True
    )

    op.create_table(
        "entity_mentions",
        _uuid_pk(),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column("surface_form", sa.Text),
        sa.Column("confidence", sa.REAL),
        sa.CheckConstraint(
            "source_kind IN ('structured','text')",
            name="entity_mentions_source_kind_valid",
        ),
        sa.CheckConstraint("source_ref <> ''", name="entity_mentions_source_ref_nonempty"),
    )
    op.create_index("entity_mentions_by_entity", "entity_mentions", ["entity_id"])

    op.create_table(
        "relations",
        _uuid_pk(),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("src_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("attributes", postgresql.JSONB),
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
        sa.UniqueConstraint("id", "build_id", name="relations_id_build_unique"),
    )
    op.create_index("relations_by_src", "relations", ["src_entity_id"])
    op.create_index("relations_by_dst", "relations", ["dst_entity_id"])
    # §17/§27.3: minted signatures are unique per build (partial — C3 stages
    # rows before C4 mints)
    op.create_index(
        "relations_by_signature",
        "relations",
        ["project", "build_id", "relation_signature"],
        unique=True,
        postgresql_where=sa.text("relation_signature IS NOT NULL"),
    )

    op.create_table(
        "relation_evidence",
        _uuid_pk(),
        sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_type", sa.Text, nullable=False),
        sa.Column("evidence_ref", sa.Text),
        # no FK: §27.4 prune survival — may dangle after the old chunk is pruned
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True)),
        sa.Column("start_offset", sa.Integer),
        sa.Column("end_offset", sa.Integer),
        sa.Column("quote", sa.Text),
        sa.Column("source_uri", sa.Text),
        # NOT NULL: a NULL hash would vacuously escape the dedup unique index
        sa.Column("evidence_hash", sa.Text, nullable=False),
        sa.Column("confidence", sa.REAL),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "evidence_type IN ('chunk','row','manual')",
            name="relation_evidence_type_valid",
        ),
        sa.CheckConstraint(
            "evidence_type <> 'chunk' OR (start_offset IS NOT NULL AND end_offset IS NOT NULL)",
            name="relation_evidence_chunk_has_span",
        ),
        sa.CheckConstraint(
            "evidence_type <> 'manual' OR (start_offset IS NULL AND end_offset IS NULL)",
            name="relation_evidence_manual_spanless",
        ),
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
        sa.CheckConstraint(
            "evidence_type <> 'row' OR (evidence_ref IS NOT NULL AND evidence_ref <> '')",
            name="relation_evidence_row_provenance",
        ),
        sa.CheckConstraint(
            "evidence_type <> 'chunk' OR (start_offset >= 0 AND end_offset >= start_offset)",
            name="relation_evidence_chunk_span_sane",
        ),
        sa.ForeignKeyConstraint(
            ["relation_id", "build_id"],
            ["relations.id", "relations.build_id"],
            ondelete="CASCADE",
            name="relation_evidence_relation_fk",
        ),
    )
    op.create_index(
        "relation_evidence_dedup",
        "relation_evidence",
        ["build_id", "evidence_hash"],
        unique=True,
    )

    op.create_table(
        "community_reports",
        _uuid_pk(),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("summary", sa.Text),
        sa.Column("member_entity_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        sa.Column("rating", sa.REAL),
    )
    op.create_index("community_reports_by_build", "community_reports", ["project", "build_id"])

    op.create_table(
        "merge_candidates",
        _uuid_pk(),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("left_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("right_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.REAL, nullable=False),
        sa.Column("features", postgresql.JSONB),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("decision", sa.Text),
        sa.Column("decided_by", sa.Text),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("reason", sa.Text),
        sa.Column("impact", postgresql.JSONB),
        sa.Column("left_snapshot", postgresql.JSONB),
        sa.Column("right_snapshot", postgresql.JSONB),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','deferred')",
            name="merge_candidates_status_valid",
        ),
        sa.CheckConstraint(
            "decision IN ('approve','reject','defer')",
            name="merge_candidates_decision_valid",
        ),
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
    op.create_index(
        "merge_candidates_by_build", "merge_candidates", ["project", "build_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("merge_candidates_by_build", table_name="merge_candidates")
    op.drop_table("merge_candidates")
    op.drop_index("community_reports_by_build", table_name="community_reports")
    op.drop_table("community_reports")
    op.drop_index("relation_evidence_dedup", table_name="relation_evidence")
    op.drop_table("relation_evidence")
    op.drop_index("relations_by_signature", table_name="relations")
    op.drop_index("relations_by_dst", table_name="relations")
    op.drop_index("relations_by_src", table_name="relations")
    op.drop_table("relations")
    op.drop_index("entity_mentions_by_entity", table_name="entity_mentions")
    op.drop_table("entity_mentions")
    op.drop_index("entities_by_key", table_name="entities")
    op.drop_table("entities")
    op.drop_index("chunks_by_document", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("documents_by_build", table_name="documents")
    op.drop_table("documents")
