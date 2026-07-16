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
        # internal, not part of the frozen Job contract (like cancel_requested):
        # the BA2d execution lease + the BA2d-2 config pin + the UXC1b eval-inputs pin.
        "lease_owner",
        "lease_expires_at",
        "config_snapshot",
        "eval_inputs_fingerprint",
    }
    assert jobs.c.id.primary_key
    # scoping / lifecycle-init columns are never null
    assert not jobs.c.project.nullable
    assert not jobs.c.kind.nullable
    assert not jobs.c.status.nullable
    assert not jobs.c.progress.nullable
    assert not jobs.c.cancel_requested.nullable
    # build_id is null until the orchestrator resolves it (§27.7); error is null
    # for an un-errored job; the lease columns are null while the job is unleased
    assert jobs.c.build_id.nullable
    assert jobs.c.error.nullable
    assert jobs.c.lease_owner.nullable
    assert jobs.c.lease_expires_at.nullable
    # config_snapshot is null until the first dispatch pins it (BA2d-2)
    assert jobs.c.config_snapshot.nullable


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
    assert {
        "jobs_status_valid",
        "jobs_progress_bounded",
        "jobs_kind_nonempty",
        "jobs_lease_paired",
        "jobs_lease_owner_nonempty",
        # 0014: a stored error is the FULL frozen Error or nothing — a partial
        # object would make GET /jobs/{id}'s pass-through contract-invalid
        "jobs_error_frozen_shape",
    } <= names


def test_offline_upgrade_sql_renders_jobs_ddl(capsys: pytest.CaptureFixture[str]) -> None:
    """The head DDL must render the 0009 table (CASCADE FK, bounded-progress /
    valid-status CHECKs) AND the 0011 lease columns + guard CHECKs — a hand-edit
    dropping one would pass the reflection tests (which read the ORM metadata, not
    the migration) yet let a bad row land on a freshly-migrated database."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head", sql=True)
    ddl = capsys.readouterr().out
    assert "CREATE TABLE jobs" in ddl
    assert "REFERENCES projects (name) ON DELETE CASCADE" in ddl
    assert "CHECK (progress >= 0 AND progress <= 1)" in ddl
    assert "status IN ('queued','running','done','failed','cancelled')" in ddl
    assert "CREATE INDEX jobs_by_project ON jobs (project, created_at DESC)" in ddl
    # 0011 execution lease
    assert "ADD COLUMN lease_owner" in ddl
    assert "ADD COLUMN lease_expires_at" in ddl
    assert "jobs_lease_paired" in ddl
    assert "jobs_lease_owner_nonempty" in ddl
    # 0012 config pin
    assert "ADD COLUMN config_snapshot" in ddl
    # 0013 reaper-scan partial index (WHERE mirrors find_reapable_jobs)
    assert "CREATE INDEX jobs_reapable" in ddl
    assert "lease_owner IS NOT NULL AND status IN ('queued','running')" in ddl
    # 0014 frozen-Error backfill + guard + queued-sweep partial index (WHERE
    # mirrors find_unenqueued_jobs)
    assert "UPDATE jobs" in ddl and "gen_random_uuid" in ddl  # legacy-error backfill
    assert "jobs_error_frozen_shape" in ddl
    assert "CREATE INDEX jobs_unenqueued" in ddl
    assert "lease_owner IS NULL AND status = 'queued'" in ddl
