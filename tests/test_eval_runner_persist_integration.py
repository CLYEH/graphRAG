"""Why: the async eval worker commits ``builds.eval`` via ``persist_build_eval`` inside
its finalize transaction (triage 17 / Finding F) — the ONE canonical eval write the §14
gate and §19 Health read. The worker component tests mock that helper, so its REAL SQL
needs a live-DB assertion: that a report round-trips to ``builds.eval``, and that a build
vanished mid-eval (a concurrent prune) raises ``LookupError`` (a report the gate can never
read must not print as success). Rolled-back txn: nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.creation import create_build
from core.config import get_settings
from core.eval.runner import EvalReport, persist_build_eval
from core.registry import create_project
from core.stores.tables import builds

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _report(build_id: uuid.UUID) -> EvalReport:
    return EvalReport(
        build_id=build_id,
        score=0.9,
        passed=3,
        failed=1,
        cases=(),
        metrics={"answer_regex": 1.0},
        fingerprint="fp",
    )


async def test_persist_build_eval_round_trips_the_report(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = f"itest-{uuid.uuid4().hex[:10]}"
            await create_project(conn, name=project)
            build_id = await create_build(conn, project, config_hash="c", source_hash="s")

            report = _report(build_id)
            await persist_build_eval(conn, report)

            stored = (
                await conn.execute(sa.select(builds.c.eval).where(builds.c.id == build_id))
            ).scalar_one()
            # the DEDICATED column holds exactly the §4 payload the gate/Health read
            assert stored == report.to_eval_payload()

            await trans.rollback()
    finally:
        await engine.dispose()


async def test_persist_build_eval_raises_when_the_build_vanished(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # a report for a build that does not exist (a concurrent prune deleted the
            # ready build before persist) matches zero rows → LookupError, so the eval
            # can't print as success on a report the gate will never read.
            with pytest.raises(LookupError, match="disappeared"):
                await persist_build_eval(conn, _report(uuid.uuid4()))
            await trans.rollback()
    finally:
        await engine.dispose()
