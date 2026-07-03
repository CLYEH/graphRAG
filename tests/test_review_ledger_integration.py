"""Why: the review_ledger DDL must hold on real Postgres — the frozen decision
and kind enums are CHECK-enforced at the database level, so a buggy writer
(C4's resolve step, Console's review endpoints) cannot corrupt the carry-
forward record with out-of-vocabulary values.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.resolve.fingerprints import FINGERPRINT_VERSION, entity_key
from core.stores.tables import review_ledger

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def test_ledger_accepts_a_valid_decision_row(migrated: None) -> None:
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await conn.execute(
                review_ledger.insert().values(
                    project=project,
                    target_kind="entity",
                    target_key=entity_key("Team", "People Ops"),
                    fingerprint_version=FINGERPRINT_VERSION,
                    decision="reject",
                    decided_by="curator-1",
                    reason="duplicate of an approved entity",
                )
            )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_out_of_vocabulary_decision_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="review_ledger_decision_valid"):
                await conn.execute(
                    review_ledger.insert().values(
                        project="itest-x",
                        target_kind="entity",
                        target_key=entity_key("Team", "X"),
                        fingerprint_version=FINGERPRINT_VERSION,
                        decision="maybe",
                        decided_by="curator-1",
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_out_of_vocabulary_kind_is_rejected(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError, match="review_ledger_kind_valid"):
                await conn.execute(
                    review_ledger.insert().values(
                        project="itest-x",
                        target_kind="document",
                        target_key=entity_key("Team", "X"),
                        fingerprint_version=FINGERPRINT_VERSION,
                        decision="reject",
                        decided_by="curator-1",
                    )
                )
            await trans.rollback()
    finally:
        await engine.dispose()
