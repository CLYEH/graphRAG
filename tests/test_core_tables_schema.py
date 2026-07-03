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
    entity_mentions,
    merge_candidates,
    relation_evidence,
    relation_evidence_dedup,
    relations,
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
    for required in ("relation_id", "build_id", "evidence_type"):
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


# --- §27.4 evidence rules as database invariants ---------------------------------


def test_chunk_evidence_must_carry_its_span() -> None:
    """§27.4: chunk evidence has a known extraction span — MUST have offsets;
    manual evidence is deliberately span-less (document-level citation)."""
    checks = _checks(relation_evidence)
    assert "start_offset IS NOT NULL" in checks["relation_evidence_chunk_has_span"]
    assert "start_offset IS NULL" in checks["relation_evidence_manual_spanless"]


def test_evidence_chunk_id_is_not_a_foreign_key() -> None:
    """§27.4 prune survival: evidence outlives the chunk it quotes (the quote/
    offsets/source_uri are denormalized), so chunk_id must be allowed to
    dangle after the old chunk is pruned — an FK would either block pruning
    or null the historical pointer."""
    assert not relation_evidence.c.chunk_id.foreign_keys


def test_evidence_dedup_is_a_database_invariant() -> None:
    """§27.4: evidence_hash exists for dedup — the hash embeds
    relation_signature, so per-build uniqueness = one row per distinct
    evidence, enforced like one_active_build."""
    assert relation_evidence_dedup.unique
    assert list(relation_evidence_dedup.columns.keys()) == ["build_id", "evidence_hash"]


# --- referential topology ---------------------------------------------------------


def test_build_scoped_graph_cascades_as_a_unit() -> None:
    """chunks→documents, mentions/relations/candidates→entities,
    evidence→relations: ON DELETE CASCADE keeps build pruning (C9) a plain
    DELETE with no orphan sweep. relation_evidence.chunk_id is the deliberate
    exception (§27.4, tested above)."""
    expected = {
        ("chunks", "document_id"): documents,
        ("entity_mentions", "entity_id"): entities,
        ("relations", "src_entity_id"): entities,
        ("relations", "dst_entity_id"): entities,
        ("relation_evidence", "relation_id"): relations,
        ("merge_candidates", "left_entity_id"): entities,
        ("merge_candidates", "right_entity_id"): entities,
    }
    tables = {
        "chunks": chunks,
        "entity_mentions": entity_mentions,
        "relations": relations,
        "relation_evidence": relation_evidence,
        "merge_candidates": merge_candidates,
    }
    for (table_name, column), parent in expected.items():
        (fk,) = tables[table_name].c[column].foreign_keys
        assert fk.column.table is parent, (table_name, column)
        assert fk.ondelete == "CASCADE", (table_name, column)


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
