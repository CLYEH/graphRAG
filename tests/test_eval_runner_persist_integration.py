"""Why: the eval-cancel path reverts ``builds.eval`` to its pre-run value via
``restore_build_eval`` (triage 16 / Finding F). The component tests mock that helper,
so its REAL SQL needs a live-DB assertion — in particular that "no prior report"
restores to a genuine SQL NULL, not the JSONB ``'null'`` LITERAL that JSONB's
``should_evaluate_none=True`` would otherwise persist and that a ``builds.eval IS NULL``
reader (§19 Health) would miscount. ``read_build_eval`` cannot tell the two apart (both
decode to Python ``None``), so the decisive assertion is the ``IS NULL`` SQL predicate.
Rolled-back txn: nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.creation import create_build
from core.config import get_settings
from core.eval.runner import read_build_eval, restore_build_eval
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


async def test_restore_build_eval_round_trips_and_no_prior_is_true_sql_null(
    migrated: None,
) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = f"itest-{uuid.uuid4().hex[:10]}"
            await create_project(conn, name=project)
            build_id = await create_build(conn, project, config_hash="c", source_hash="s")

            # a fresh build has SQL NULL eval; read_build_eval surfaces that as None
            assert await read_build_eval(conn, build_id) is None

            # simulate run_eval's write, then confirm read_build_eval round-trips it
            report = {"score": 0.9, "passed": 3, "metrics": {"answer_regex": 1.0}}
            await conn.execute(
                builds.update()
                .where(builds.c.id == build_id)
                .values(eval=sa.cast(report, postgresql.JSONB))
            )
            assert await read_build_eval(conn, build_id) == report

            # the cancel path WITH a prior restores THAT exact dict (not a wipe)
            prior = {"score": 0.5, "was_prior": True}
            await restore_build_eval(conn, build_id, prior)
            assert await read_build_eval(conn, build_id) == prior

            # …and with NO prior restores GENUINE SQL NULL, not the JSONB 'null'
            # literal: `eval IS NULL` matches SQL NULL but NOT 'null'::jsonb, so this
            # is the revert-probe for the should_evaluate_none bug.
            await restore_build_eval(conn, build_id, None)
            matched_is_null = (
                await conn.execute(
                    sa.select(sa.literal(1)).where(builds.c.id == build_id, builds.c.eval.is_(None))
                )
            ).scalar_one_or_none()
            assert matched_is_null == 1  # true SQL NULL ('null'::jsonb would NOT match IS NULL)

            await trans.rollback()
    finally:
        await engine.dispose()
