"""Why: DR-006's promise is structural — a caller CANNOT produce an unscoped
read or a cross-build write through this layer, and tables whose scoping is a
lie (cross-build by design, or only transitively scoped) are rejected instead
of faked. These tests pin that structure at the SQL level; the integration
tests prove it against live Postgres.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables
from core.stores.repo import (
    BUILD_ONLY_SCOPED,
    PROJECT_AND_BUILD_SCOPED,
    BuildScopedRepo,
    NoActiveBuildError,
    NotBuildScopedError,
)

_BUILD = uuid.uuid4()


def _repo() -> BuildScopedRepo:
    # unit tests never execute SQL, so the connection is a placeholder
    return BuildScopedRepo(conn=cast(AsyncConnection, object()), project="p1", build_id=_BUILD)


def test_reads_inject_both_scope_columns() -> None:
    """§27.1: reads automatically filter build_id (and project where the
    table carries it) — the caller cannot forget what it never had to add."""
    for table in PROJECT_AND_BUILD_SCOPED:
        sql = str(_repo().select(table))
        assert f"{table.name}.build_id = " in sql, table.name
        assert f"{table.name}.project = " in sql, table.name


def test_build_only_tables_inject_build_id() -> None:
    """chunks/relation_evidence carry no project column (§4) — their project
    is derivable through the composite FK parent; the repo scopes what the
    table actually has."""
    for table in BUILD_ONLY_SCOPED:
        sql = str(_repo().select(table))
        assert f"{table.name}.build_id = " in sql, table.name
        assert "project" not in sql, table.name


@pytest.mark.parametrize(
    "table",
    [tables.builds, tables.review_ledger, tables.entity_mentions, tables.pipeline_runs],
    ids=lambda t: str(t.name),
)
def test_unscopable_tables_are_rejected_loudly(table: sa.Table) -> None:
    """builds is the scope's source of truth; review_ledger is deliberately
    cross-build (DR-003); pipeline_runs has its own §27.7 binding rules;
    entity_mentions are scoped only through their entity. Pretending to scope
    any of these would fake the DR-006 guarantee, so both paths refuse."""
    with pytest.raises(NotBuildScopedError):
        _repo().select(table)
    with pytest.raises(NotBuildScopedError):
        _repo().insert_values(table, project="p1")


def test_writes_inject_the_bound_scope() -> None:
    """§27.1: 寫入一律指定 building 的 build_id — the repo bound to that build
    injects it, so a pipeline writer cannot land rows in another build."""
    insert = _repo().insert_values(tables.documents, source_uri="s3://d", content_hash="c")
    compiled = insert.compile()
    assert compiled.params["build_id"] == _BUILD
    assert compiled.params["project"] == "p1"


def test_conflicting_explicit_scope_is_a_loud_bug() -> None:
    """A caller passing a DIFFERENT build_id/project than the binding is a
    cross-build write either way it would be resolved — reject, don't pick."""
    with pytest.raises(ValueError, match="conflict"):
        _repo().insert_values(tables.documents, build_id=uuid.uuid4(), source_uri="s")
    # matching explicit values are redundant but not a bug
    insert = _repo().insert_values(tables.documents, build_id=_BUILD, source_uri="s")
    assert insert.compile().params["build_id"] == _BUILD


def test_repo_binding_is_immutable() -> None:
    """The binding IS the §27.1 per-request cache — a mutable scope would
    reintroduce mid-request mixed-version reads."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        _repo().build_id = uuid.uuid4()  # type: ignore[misc]


def test_no_active_build_error_is_typed_and_carries_the_project() -> None:
    """The API layer maps this to the frozen NO_ACTIVE_BUILD code (§15) — it
    needs the type and the project, not a string to parse."""
    err = NoActiveBuildError("p1")
    assert err.project == "p1"
    assert isinstance(err, LookupError)
