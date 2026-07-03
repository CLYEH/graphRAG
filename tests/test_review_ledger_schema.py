"""Why: DR-003's carry-forward depends on review_ledger being NON-build-scoped
with fingerprint-versioned keys (§27.3/DR-007). These tests pin the table
shape and prove the rendered migration DDL carries the frozen enums — before
C4 starts writing decisions through it.
"""

from __future__ import annotations

import sqlalchemy as sa

from core.stores.tables import review_ledger, review_ledger_lookup


def test_review_ledger_is_not_build_scoped() -> None:
    """The whole point of DR-003: no build_id column — decisions outlive builds."""
    assert "build_id" not in {c.name for c in review_ledger.columns}


def test_review_ledger_columns_match_design_spec() -> None:
    expected = {
        "id",
        "project",
        "target_kind",
        "target_key",
        "fingerprint_version",
        "decision",
        "decided_by",
        "decided_at",
        "reason",
    }
    assert {c.name for c in review_ledger.columns} == expected
    for required in (
        "project",
        "target_kind",
        "target_key",
        "fingerprint_version",
        "decision",
        "decided_by",
        "decided_at",
    ):
        assert not review_ledger.c[required].nullable, required


def test_frozen_enums_are_check_constrained() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in review_ledger.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    for kind in ("entity", "relation", "merge"):
        assert f"'{kind}'" in checks["review_ledger_kind_valid"]
    for decision in ("approve", "reject", "defer", "merge", "split"):
        assert f"'{decision}'" in checks["review_ledger_decision_valid"]


def test_lookup_index_covers_the_resolve_query_path() -> None:
    assert list(review_ledger_lookup.columns.keys()) == [
        "project",
        "target_kind",
        "target_key",
        "fingerprint_version",
    ]
