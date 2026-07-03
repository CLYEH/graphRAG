"""Why: §18's promise is that a failed build is *diagnosable and resumable* —
Console can say "failed at graph, 3 docs" and retry only those. That only
works if the three layers keep their §4 shape, runs stay serializable through
the frozen jobs surface (§15 JobStatus), and the §27.7 binding/dedup rules are
database invariants rather than writer discipline — pinned here before C2+
starts writing rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from core.observability.spec import SOURCE_VALIDATION_RUN_KIND
from core.stores.tables import (
    pipeline_runs,
    pipeline_step_items,
    pipeline_step_items_dedup,
    pipeline_steps,
    pipeline_steps_by_run,
)

_OPENAPI = Path(__file__).resolve().parent.parent / "contracts" / "openapi.yaml"


def _checks(table: sa.Table) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint) and isinstance(c.name, str)
    }


def test_pipeline_runs_columns_match_design_spec() -> None:
    expected = {
        "id",
        "project",
        "build_id",
        "kind",
        "status",
        "config_hash",
        "source_hash",
        "created_by",
        "started_at",
        "finished_at",
        "metrics",
        "error",
    }
    assert {c.name for c in pipeline_runs.columns} == expected
    for required in ("project", "kind", "status"):
        assert not pipeline_runs.c[required].nullable, required


def test_run_build_id_stays_nullable_for_source_validation_jobs() -> None:
    """§27.7 defines a legitimate null: pure source-validation jobs carry no
    build. NOT NULL here would make that case unrepresentable."""
    assert pipeline_runs.c.build_id.nullable


def test_null_build_id_is_reserved_for_source_validation() -> None:
    """§27.7 is exhaustive about the null boundary: the pure source-validation
    job is the ONLY build-unbound kind — a CHECK that merely special-cased
    ingest would admit e.g. a build/reproject run missing its build_id, an
    orphan row the retry boundary could never merge back into a build."""
    sqltext = _checks(pipeline_runs)["pipeline_runs_build_binding"]
    assert "build_id IS NOT NULL" in sqltext
    assert f"'{SOURCE_VALIDATION_RUN_KIND}'" in sqltext  # single-sourced from the spec


def test_pipeline_steps_columns_match_design_spec() -> None:
    expected = {
        "id",
        "run_id",
        "step_name",
        "status",
        "started_at",
        "finished_at",
        "input_count",
        "output_count",
        "skipped_count",
        "failed_count",
        "metrics",
        "error",
    }
    assert {c.name for c in pipeline_steps.columns} == expected
    for required in ("run_id", "step_name", "status"):
        assert not pipeline_steps.c[required].nullable, required


def test_pipeline_step_items_columns_match_design_spec() -> None:
    expected = {"id", "step_id", "item_kind", "item_ref", "status", "message", "error"}
    assert {c.name for c in pipeline_step_items.columns} == expected
    for required in ("step_id", "item_kind", "item_ref", "status"):
        assert not pipeline_step_items.c[required].nullable, required


def test_layers_are_linked_and_prunable_as_a_unit() -> None:
    """Steps belong to runs, items to steps; ON DELETE CASCADE keeps §18
    retention (🔧 item_retention_days) a plain DELETE with no orphan sweep."""
    (step_fk,) = pipeline_steps.c.run_id.foreign_keys
    assert step_fk.column.table is pipeline_runs
    assert step_fk.ondelete == "CASCADE"
    (item_fk,) = pipeline_step_items.c.step_id.foreign_keys
    assert item_fk.column.table is pipeline_steps
    assert item_fk.ondelete == "CASCADE"
    assert list(pipeline_steps_by_run.columns.keys()) == ["run_id"]


def test_item_dedup_is_a_database_invariant() -> None:
    """§27.7: retry idempotency rests on item_ref dedup — within one step an
    item has exactly one outcome row, enforced by a unique index (the same
    pattern as one_active_build), not by writer discipline."""
    assert pipeline_step_items_dedup.unique
    assert list(pipeline_step_items_dedup.columns.keys()) == [
        "step_id",
        "item_kind",
        "item_ref",
    ]


def test_item_identifiers_reject_the_empty_string() -> None:
    """H6 (the identifier rule, applied retroactively to this P6 table): ''
    is a no-op identity — such rows would collide under the dedup index and
    the §27.7 retry set could never name the work they stand for."""
    checks = _checks(pipeline_step_items)
    assert "item_kind <> ''" in checks["pipeline_step_items_kind_nonempty"]
    assert "item_ref <> ''" in checks["pipeline_step_items_ref_nonempty"]


@pytest.mark.contract
def test_run_status_enum_is_in_lockstep_with_the_frozen_jobs_contract() -> None:
    """Runs surface through the jobs API (§15/BA2): every status the table can
    store must be serializable as the frozen JobStatus enum (§27.2), and every
    contract status must be storable — a fork either way strands rows or
    payloads one side can't process."""
    spec = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    job_statuses = spec["components"]["schemas"]["JobStatus"]["enum"]
    sqltext = _checks(pipeline_runs)["pipeline_runs_status_valid"]
    stored = {token.strip() for token in sqltext.split("(")[1].rstrip(")").split(",")}
    # membership both ways, order-insensitive — SQL IN doesn't care about order
    assert stored == {f"'{s}'" for s in job_statuses}
