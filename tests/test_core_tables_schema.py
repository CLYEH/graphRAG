"""Why: these tables are the Postgres source of record every projection and
API payload derives from (DR-006). The shape must match DESIGN §4, the enum
vocabularies must stay in lockstep with the frozen contract (a fork strands
rows the API can't serialize — or payloads the DB can't store), and §27.4's
evidence rules (spans by type, dedup, prune survival) must be database
invariants before C2+ starts writing rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml

from core.stores.tables import (
    CREATED_BY,
    EVIDENCE_TYPES,
    LIFECYCLE_STATUSES,
    MERGE_CANDIDATE_DECISIONS,
    MERGE_CANDIDATE_STATUSES,
    REVIEW_STATUSES,
    chunks,
    community_reports,
    documents,
    entities,
    entities_by_key,
    entity_mentions,
    merge_candidates,
    relation_evidence,
    relation_evidence_dedup,
    relations,
    relations_by_signature,
)

_OPENAPI = Path(__file__).resolve().parent.parent / "contracts" / "openapi.yaml"


def _checks(table: sa.Table) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint) and isinstance(c.name, str)
    }


def _cols(table: sa.Table) -> set[str]:
    return {c.name for c in table.columns}


# --- table shapes match DESIGN §4 ----------------------------------------------


def test_documents_columns_match_design_spec() -> None:
    assert _cols(documents) == {
        "id",
        "project",
        "build_id",
        "source_uri",
        "raw",
        "content_hash",
        "mime",
        "metadata",
        "status",
        "ingested_at",
    }
    for required in ("project", "build_id", "source_uri", "content_hash"):
        assert not documents.c[required].nullable, required


def test_chunks_columns_match_design_spec() -> None:
    assert _cols(chunks) == {
        "id",
        "document_id",
        "build_id",
        "ordinal",
        "text",
        "token_count",
        "start_offset",
        "end_offset",
        "vector_point_id",
        "metadata",
        "status",
    }
    for required in ("document_id", "build_id", "ordinal", "text"):
        assert not chunks.c[required].nullable, required


def test_entities_columns_match_design_spec() -> None:
    assert _cols(entities) == {
        "id",
        "project",
        "build_id",
        "type",
        "canonical_name",
        "entity_key",
        "attributes",
        "embedding_point_id",
        "status",
        "review_status",
        "created_by",
        "created_at",
        "updated_at",
    }
    for required in ("project", "build_id", "type", "canonical_name", "entity_key", "status"):
        assert not entities.c[required].nullable, required


def test_entity_mentions_columns_match_design_spec() -> None:
    assert _cols(entity_mentions) == {
        "id",
        "entity_id",
        "source_kind",
        "source_ref",
        "surface_form",
        "confidence",
    }
    for required in ("entity_id", "source_kind", "source_ref"):
        assert not entity_mentions.c[required].nullable, required


def test_relations_columns_match_design_spec() -> None:
    assert _cols(relations) == {
        "id",
        "project",
        "build_id",
        "src_entity_id",
        "dst_entity_id",
        "type",
        "attributes",
        "relation_signature",
        "status",
        "review_status",
        "created_by",
        "confidence",
        "created_at",
        "updated_at",
    }
    for required in ("project", "build_id", "src_entity_id", "dst_entity_id", "type", "status"):
        assert not relations.c[required].nullable, required


def test_relation_evidence_columns_match_design_spec() -> None:
    """Includes source_uri: §4's terse list omits it, but §27.4 (prune
    survival denormalizes quote/offsets/source_uri) and the frozen P1
    RelationEvidence contract field both require it — §4 was synced in C1a."""
    assert _cols(relation_evidence) == {
        "id",
        "relation_id",
        "build_id",
        "evidence_type",
        "evidence_ref",
        "chunk_id",
        "start_offset",
        "end_offset",
        "quote",
        "source_uri",
        "evidence_hash",
        "confidence",
        "created_at",
    }
    for required in ("relation_id", "build_id", "evidence_type", "evidence_hash"):
        assert not relation_evidence.c[required].nullable, required


def test_community_reports_columns_match_design_spec() -> None:
    assert _cols(community_reports) == {
        "id",
        "project",
        "build_id",
        "level",
        "title",
        "summary",
        "member_entity_ids",
        "rating",
    }


def test_merge_candidates_columns_match_design_spec() -> None:
    assert _cols(merge_candidates) == {
        "id",
        "project",
        "build_id",
        "left_entity_id",
        "right_entity_id",
        "score",
        "features",
        "status",
        "decision",
        "decided_by",
        "decided_at",
        "reason",
        "impact",
        "left_snapshot",
        "right_snapshot",
    }
    for required in ("project", "build_id", "left_entity_id", "right_entity_id", "score", "status"):
        assert not merge_candidates.c[required].nullable, required


# --- §17/§27.3 identity invariants -------------------------------------------------


def test_entity_key_is_unique_per_build() -> None:
    """§17/§27.3: entity_key IS the canonical identity — two rows sharing a
    key in one build would make the review ledger apply a single decision to
    several entities and projections carry duplicate identities."""
    assert entities_by_key.unique
    assert list(entities_by_key.columns.keys()) == ["project", "build_id", "entity_key"]


def test_minted_relation_signature_is_unique_per_build() -> None:
    """Same invariant for relations, partial on purpose: C3 stages extracted
    rows before C4 mints signatures (NULL allowed), but once minted the
    identity must be unique or ledger application forks."""
    assert relations_by_signature.unique
    where = relations_by_signature.dialect_options["postgresql"]["where"]
    assert "IS NOT NULL" in str(where)


# --- §27.4 evidence rules as database invariants ---------------------------------


def test_chunk_evidence_must_carry_its_span() -> None:
    """§27.4: chunk evidence has a known extraction span — MUST have offsets;
    manual evidence is deliberately span-less (document-level citation)."""
    checks = _checks(relation_evidence)
    assert "start_offset IS NOT NULL" in checks["relation_evidence_chunk_has_span"]
    assert "start_offset IS NULL" in checks["relation_evidence_manual_spanless"]


def test_each_evidence_type_must_carry_its_provenance() -> None:
    """The frozen MCP relation source-ref contract (mcp_response.schema.json)
    requires chunk refs to emit source_uri+quote+offsets, document/manual refs
    source_uri+quote, and row refs table+pk (evidence_ref) — all with
    minLength 1. A stored row missing (or blanking) its type's provenance
    could never produce a contract-valid ref once the source chunk is pruned
    (§27.4), so the SoR rejects it at write time."""
    checks = _checks(relation_evidence)
    for name in ("relation_evidence_chunk_provenance", "relation_evidence_manual_provenance"):
        assert "quote IS NOT NULL" in checks[name]
        assert "source_uri IS NOT NULL" in checks[name]
        assert "<> ''" in checks[name]  # contract minLength 1 — empty ≠ provided
    assert "evidence_ref IS NOT NULL" in checks["relation_evidence_row_provenance"]


def test_evidence_chunk_id_is_not_a_foreign_key() -> None:
    """§27.4 prune survival: evidence outlives the chunk it quotes (the quote/
    offsets/source_uri are denormalized), so chunk_id must be allowed to
    dangle after the old chunk is pruned — an FK would either block pruning
    or null the historical pointer."""
    assert not relation_evidence.c.chunk_id.foreign_keys


def test_evidence_dedup_is_a_database_invariant() -> None:
    """§27.4: evidence_hash exists for dedup — the hash embeds
    relation_signature, so per-build uniqueness = one row per distinct
    evidence, enforced like one_active_build. NOT NULL is part of the
    invariant: Postgres unique indexes treat NULLs as distinct, so a nullable
    hash would let hashless rows duplicate freely (the no-op-value escape,
    lesson class 1)."""
    assert relation_evidence_dedup.unique
    assert list(relation_evidence_dedup.columns.keys()) == ["build_id", "evidence_hash"]
    assert not relation_evidence.c.evidence_hash.nullable


# --- referential topology ---------------------------------------------------------


def test_child_fks_are_build_aligned_and_cascade() -> None:
    """DR-006: a child row must provably live in its parent's build (and
    project where both sides carry it) — the FKs are COMPOSITE on build_id/
    project, so cross-build mixing and cross-build cascade deletes are
    unrepresentable. ON DELETE CASCADE keeps build pruning (C9) a plain
    DELETE. relation_evidence.chunk_id is the deliberate FK exception
    (§27.4, tested above); entity_mentions carry no build/project columns,
    so their FK is plain id."""
    expected: dict[str, list[tuple[list[str], sa.Table]]] = {
        "chunks": [(["document_id", "build_id"], documents)],
        "entity_mentions": [(["entity_id"], entities)],
        "relations": [
            (["src_entity_id", "project", "build_id"], entities),
            (["dst_entity_id", "project", "build_id"], entities),
        ],
        "relation_evidence": [(["relation_id", "build_id"], relations)],
        "merge_candidates": [
            (["left_entity_id", "project", "build_id"], entities),
            (["right_entity_id", "project", "build_id"], entities),
        ],
    }
    tables = {
        "chunks": chunks,
        "entity_mentions": entity_mentions,
        "relations": relations,
        "relation_evidence": relation_evidence,
        "merge_candidates": merge_candidates,
    }
    for table_name, fk_specs in expected.items():
        constraints = list(tables[table_name].foreign_key_constraints)
        assert len(constraints) == len(fk_specs), table_name
        actual = {
            (tuple(fkc.column_keys), fkc.referred_table.name, fkc.ondelete) for fkc in constraints
        }
        for columns, parent in fk_specs:
            assert (tuple(columns), parent.name, "CASCADE") in actual, (table_name, columns)


def test_chunk_evidence_span_must_be_sane() -> None:
    """The frozen MCP contract pins offsets at minimum 0, and after prune the
    denormalized span is the only auditable citation left — a negative or
    inverted range could never be emitted as a valid ref."""
    sqltext = _checks(relation_evidence)["relation_evidence_chunk_span_sane"]
    assert "start_offset >= 0" in sqltext
    assert "end_offset >= start_offset" in sqltext


# --- enum lockstep with the frozen contract (DR-002) ------------------------------


@pytest.fixture(scope="module")
def openapi_schemas() -> dict[str, Any]:
    spec = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    schemas: dict[str, Any] = spec["components"]["schemas"]
    return schemas


@pytest.mark.contract
def test_frozen_enum_tuples_match_the_contract(openapi_schemas: dict[str, Any]) -> None:
    """The Python tuples ARE what the CHECK literals and future writers use —
    they must mirror the frozen contract enums exactly (class-2 lockstep)."""
    assert tuple(openapi_schemas["LifecycleStatus"]["enum"]) == LIFECYCLE_STATUSES
    assert tuple(openapi_schemas["ReviewStatus"]["enum"]) == REVIEW_STATUSES
    assert tuple(openapi_schemas["CreatedBy"]["enum"]) == CREATED_BY
    assert tuple(openapi_schemas["MergeCandidateStatus"]["enum"]) == MERGE_CANDIDATE_STATUSES
    assert (
        tuple(openapi_schemas["RelationEvidence"]["properties"]["evidence_type"]["enum"])
        == EVIDENCE_TYPES
    )
    # the contract's nullable enum carries a JSON null alongside the values
    decision_enum = openapi_schemas["MergeCandidate"]["properties"]["decision"]["enum"]
    assert tuple(v for v in decision_enum if v is not None) == MERGE_CANDIDATE_DECISIONS


@pytest.mark.contract
def test_check_constraints_carry_the_frozen_enums() -> None:
    """The CHECK literals must contain every tuple member — a rename that
    updates the tuple but not the DDL (or vice versa) fails here."""
    cases: list[tuple[sa.Table, str, tuple[str, ...]]] = [
        (entities, "entities_status_valid", LIFECYCLE_STATUSES),
        (entities, "entities_review_status_valid", REVIEW_STATUSES),
        (entities, "entities_created_by_valid", CREATED_BY),
        (relations, "relations_status_valid", LIFECYCLE_STATUSES),
        (relations, "relations_review_status_valid", REVIEW_STATUSES),
        (relations, "relations_created_by_valid", CREATED_BY),
        (relation_evidence, "relation_evidence_type_valid", EVIDENCE_TYPES),
        (merge_candidates, "merge_candidates_status_valid", MERGE_CANDIDATE_STATUSES),
        (merge_candidates, "merge_candidates_decision_valid", MERGE_CANDIDATE_DECISIONS),
    ]
    for table, check_name, values in cases:
        sqltext = _checks(table)[check_name]
        for value in values:
            assert f"'{value}'" in sqltext, (check_name, value)


@pytest.mark.contract
def test_free_string_statuses_stay_unchecked(openapi_schemas: dict[str, Any]) -> None:
    """documents.status / chunks.status are free strings in the frozen
    contract — a DB CHECK would reject rows the contract calls legitimate
    (the P6/#15 over-tightening lesson, inverted)."""
    for schema_name in ("Document", "Chunk"):
        assert "enum" not in openapi_schemas[schema_name]["properties"]["status"]
    doc_checks = _checks(documents)
    chunk_checks = _checks(chunks)
    assert not any("status" in text for text in doc_checks.values())
    assert not any("status" in text for text in chunk_checks.values())
