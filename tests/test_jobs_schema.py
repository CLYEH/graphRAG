"""Why: the ``jobs`` table is the durable SoR the Console serves for
GET /jobs/{id}, so its ``status`` must stay serializable as the frozen §15
JobStatus enum (a fork would hand the API a row it can't render), its columns
must cover the contract Job shape, and its guard CHECKs (bounded progress,
non-empty kind) must be DB invariants, not writer discipline. Pinned before the
worker (BA2c) and endpoints (BA2d) start writing rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from alembic import command
from alembic.config import Config

from core.stores.tables import jobs

REPO_ROOT = Path(__file__).resolve().parent.parent
_OPENAPI = REPO_ROOT / "contracts" / "openapi.yaml"


def _checks(table: sa.Table) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint) and isinstance(c.name, str)
    }


def test_jobs_columns_cover_the_contract_job_shape() -> None:
    assert {c.name for c in jobs.columns} == {
        "id",
        "project",
        "kind",
        "build_id",
        "status",
        "step",
        "progress",
        "message",
        "error",
        "cancel_requested",
        "created_at",
        "finished_at",
    }
    assert jobs.c.id.primary_key
    # scoping / lifecycle-init columns are never null
    assert not jobs.c.project.nullable
    assert not jobs.c.kind.nullable
    assert not jobs.c.status.nullable
    assert not jobs.c.progress.nullable
    assert not jobs.c.cancel_requested.nullable
    # build_id is null until the orchestrator resolves it (§27.7); error is null
    # for an un-errored job
    assert jobs.c.build_id.nullable
    assert jobs.c.error.nullable


def test_jobs_project_fk_cascades() -> None:
    fks = list(jobs.c.project.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "projects"
    assert fks[0].ondelete == "CASCADE"


def test_jobs_status_enum_is_in_lockstep_with_the_frozen_contract() -> None:
    """A row's status must be exactly the frozen §15 JobStatus vocabulary — a
    drift would let the store hold a value GET /jobs/{id} cannot serialize."""
    spec = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    job_statuses = spec["components"]["schemas"]["JobStatus"]["enum"]
    sqltext = _checks(jobs)["jobs_status_valid"]
    stored = {token.strip() for token in sqltext.split("(")[1].rstrip(")").split(",")}
    assert stored == {f"'{s}'" for s in job_statuses}


def test_jobs_guard_checks_present() -> None:
    names = set(_checks(jobs))
    assert {"jobs_status_valid", "jobs_progress_bounded", "jobs_kind_nonempty"} <= names


def test_offline_upgrade_sql_renders_jobs_ddl(capsys: pytest.CaptureFixture[str]) -> None:
    """The 0009 migration must render the table, the CASCADE FK, and the bounded
    -progress / valid-status CHECKs — a hand-edit dropping one would pass the
    column tests yet let a bad row land."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head", sql=True)
    ddl = capsys.readouterr().out
    assert "CREATE TABLE jobs" in ddl
    assert "REFERENCES projects (name) ON DELETE CASCADE" in ddl
    assert "CHECK (progress >= 0 AND progress <= 1)" in ddl
    assert "status IN ('queued','running','done','failed','cancelled')" in ddl
    assert "CREATE INDEX jobs_by_project ON jobs (project, created_at DESC)" in ddl
