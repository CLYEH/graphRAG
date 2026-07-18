"""Why: BA1's control-plane registry (projects/sources) is the parent the whole
build lifecycle hangs off. These unit tests pin the table shapes against the
frozen contract Project/Source schemas and pin the one structural guarantee the
router relies on — deleting a project cascades its sources (ON DELETE CASCADE),
so a project delete can never orphan sources. No DB needed; the migration's
rendered DDL is asserted offline so a hand-edit that drops the cascade or a
CHECK is caught before it ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from core.stores.tables import idempotency_keys, projects, sources

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    return Config(str(REPO_ROOT / "alembic.ini"))


def test_projects_columns_match_contract() -> None:
    assert {c.name for c in projects.columns} == {
        "name",
        "display_name",
        "description",
        "config",
        "created_at",
    }
    assert projects.c.name.primary_key
    # config/created_at are contract-required → never null
    assert not projects.c.config.nullable
    assert not projects.c.created_at.nullable
    # the optional fields stay nullable
    assert projects.c.display_name.nullable
    assert projects.c.description.nullable


def test_sources_columns_match_contract() -> None:
    assert {c.name for c in sources.columns} == {
        "id",
        "project",
        "kind",
        "uri",
        "metadata",
        "added_at",
        "enabled",  # SRC2 (DR-013)
    }
    assert sources.c.id.primary_key
    assert not sources.c.project.nullable
    assert not sources.c.uri.nullable
    assert not sources.c.metadata.nullable
    assert sources.c.kind.nullable  # contract Source requires only [id, uri]
    assert not sources.c.enabled.nullable  # NOT NULL + server_default true (no backfill)


def test_sources_project_fk_cascades() -> None:
    """The parent/child integrity that lets delete_project rely on the DB to
    remove sources — not application code that could forget."""
    fks = list(sources.c.project.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column is projects.c.name
    assert fks[0].ondelete == "CASCADE"


def test_nonempty_checks_present() -> None:
    names = {c.name for c in projects.constraints if isinstance(c, sa.CheckConstraint)}
    assert "projects_name_nonempty" in names
    src = {c.name for c in sources.constraints if isinstance(c, sa.CheckConstraint)}
    assert "sources_uri_nonempty" in src


def test_idempotency_keys_columns() -> None:
    assert {c.name for c in idempotency_keys.columns} == {
        "key",
        "project",
        "endpoint",
        "request_hash",
        "response",
        "status",
        "created_at",
        "expires_at",
    }
    assert idempotency_keys.c.key.primary_key  # the PK serializes concurrent same-key reqs
    assert not idempotency_keys.c.expires_at.nullable  # every key has a TTL window
    # response/status are filled AFTER the handler (reserve-first) → nullable
    assert idempotency_keys.c.response.nullable
    assert idempotency_keys.c.status.nullable
    # the reserve-or-filled invariant is enforced structurally, not by handler code
    checks = {c.name for c in idempotency_keys.constraints if isinstance(c, sa.CheckConstraint)}
    assert "idempotency_keys_reserve_or_filled" in checks


def test_offline_upgrade_sql_renders_registry_ddl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 0007 migration must render the tables, the cascade FK, and the
    non-empty CHECKs — a migration that built the tables but silently dropped
    the ON DELETE CASCADE would pass every column test yet let a project delete
    orphan its sources."""
    command.upgrade(_alembic_config(), "head", sql=True)
    ddl = capsys.readouterr().out
    assert "CREATE TABLE projects" in ddl
    assert "CREATE TABLE sources" in ddl
    # the cascade must be bound to THIS FK — a bare `"ON DELETE CASCADE" in ddl`
    # is always true (pre-existing C1a tables render it), so assert the whole
    # sources→projects clause, which only appears if this FK carries it
    assert "REFERENCES projects (name) ON DELETE CASCADE" in ddl
    assert "CHECK (name <> '')" in ddl
    assert "CHECK (uri <> '')" in ddl
    assert "CREATE INDEX sources_by_project ON sources (project)" in ddl
    # the registry must be backfilled from pre-registry project-keyed tables,
    # or existing projects vanish from list/get while their builds still resolve
    assert "INSERT INTO projects (name)" in ddl
    assert "FROM builds" in ddl
    # the §27 idempotency store (0008) — key PK, non-empty CHECK, and the
    # reserve-or-filled invariant (never a half-filled row)
    assert "CREATE TABLE idempotency_keys" in ddl
    assert "PRIMARY KEY (key)" in ddl
    assert "CHECK (key <> '')" in ddl
    assert "CHECK ((status IS NULL) = (response IS NULL))" in ddl
