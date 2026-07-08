"""Why: DR-001/DR-006 rest on one database invariant — at most ONE active build
per project — delivered by the `one_active_build` partial unique index, with
§4's status lifecycle frozen as a CHECK constraint. These tests pin the schema
definition and the migration chain so the invariant cannot silently drift
before C1 builds the repository layer on top of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from core.stores.tables import BUILD_STATUSES, builds, one_active_build

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    return Config(str(REPO_ROOT / "alembic.ini"))


def test_migration_chain_has_a_single_head() -> None:
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    assert heads == ["0011_jobs_lease"]


def test_builds_columns_match_design_spec() -> None:
    expected = {
        "id",
        "project",
        "status",
        "config_hash",
        "source_hash",
        "started_at",
        "finished_at",
        "activated_at",
        "metrics",
        "eval",
    }
    assert {c.name for c in builds.columns} == expected
    assert builds.c.id.primary_key
    assert not builds.c.project.nullable
    assert not builds.c.status.nullable


def test_status_lifecycle_is_frozen() -> None:
    assert BUILD_STATUSES == ("building", "ready", "active", "failed", "archived")
    check = next(
        c
        for c in builds.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == "builds_status_valid"
    )
    for status in BUILD_STATUSES:
        assert f"'{status}'" in str(check.sqltext)


def test_one_active_build_is_a_partial_unique_index() -> None:
    assert one_active_build.unique
    assert list(one_active_build.columns.keys()) == ["project"]
    where = one_active_build.dialect_options["postgresql"]["where"]
    assert str(where) == "status = 'active'"


def test_builds_project_fk_restricts_deletion() -> None:
    """BA2b: builds.project → projects.name RESTRICT — a build can't exist
    without its project, and a project with builds can't be deleted (the DB
    backstop under delete_project). RESTRICT, never CASCADE (a project delete
    must go through the multi-store build sweep)."""
    fks = list(builds.c.project.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "projects"
    assert fks[0].ondelete == "RESTRICT"


def test_offline_upgrade_sql_creates_the_partial_index(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The migration must render the exact §27.1 DDL — a migration that builds
    the table but silently drops the WHERE clause would still pass every other
    test while voiding the DR-001 guarantee."""
    command.upgrade(_alembic_config(), "head", sql=True)
    ddl = capsys.readouterr().out
    assert "CREATE TABLE builds" in ddl
    assert "CREATE UNIQUE INDEX one_active_build ON builds (project)" in ddl
    assert "WHERE status = 'active'" in ddl
    assert "CHECK (status IN ('building','ready','active','failed','archived'))" in ddl
    # the FK is added by a later migration (0010) via ALTER TABLE, not in the
    # CREATE — assert the RESTRICT FK + its supporting index render so a
    # dropped-FK/index migration is caught
    assert "ADD CONSTRAINT builds_project_fkey FOREIGN KEY(project)" in ddl
    assert "REFERENCES projects (name) ON DELETE RESTRICT" in ddl
    assert "CREATE INDEX builds_by_project ON builds (project)" in ddl
