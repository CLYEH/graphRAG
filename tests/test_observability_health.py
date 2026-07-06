"""Why: Health is §19's operator-facing verdict — the status light must obey
the documented precedence (most actionable wins), drift must come from the
SAME checker preflight uses (one checker, no class-5 fork), and the eval
light must only report MEASURED, COMPARABLE facts. Live stores; empty builds
make the drift check trivially agree so each light is isolated."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.observability.health import health_report
from core.resolve import fingerprints
from core.stores import tables
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def project(migrated: None) -> AsyncIterator[str]:
    name = f"health-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(tables.entities.delete().where(tables.entities.c.project == name))
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == name))
        await conn.commit()
    await engine.dispose()


async def test_health_lights_follow_the_documented_precedence(project: str) -> None:
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            # no builds at all → Healthy (nothing to report on)
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Healthy"
            assert report.metrics["builds_total"] == 0

            # an ACTIVE empty build → still Healthy; content metrics appear
            active_id: uuid.UUID = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="active", started_at=NOW)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Healthy"
            assert report.metrics["entities"] == 0 and report.drift == ()

            # pending merge candidate → Needs review. Direct SQL fixture —
            # the writer's building-only fence is correct and not under test
            left, right = uuid.uuid4(), uuid.uuid4()
            for eid, name in ((left, "Acme"), (right, "Acme Corp")):
                await conn.execute(
                    tables.entities.insert().values(
                        id=eid,
                        project=project,
                        build_id=active_id,
                        type="org",
                        canonical_name=name,
                        entity_key=fingerprints.entity_key("org", name),
                        status="merged",  # NOT active — drift check stays 0==0
                        review_status="unreviewed",
                        created_by="rule",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            await conn.execute(
                tables.merge_candidates.insert().values(
                    id=uuid.uuid4(),
                    project=project,
                    build_id=active_id,
                    left_entity_id=left,
                    right_entity_id=right,
                    score=0.9,
                    status="pending",
                )
            )
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Needs review"
            assert report.metrics["pending_review"] == 1

            # an ACTIVE entity in PG only → the SAME drift checker preflight
            # uses fires → Index drift outranks Needs review
            await conn.execute(
                tables.entities.insert().values(
                    id=uuid.uuid4(),
                    project=project,
                    build_id=active_id,
                    type="org",
                    canonical_name="Globex",
                    entity_key=fingerprints.entity_key("org", "Globex"),
                    status="active",
                    review_status="unreviewed",
                    created_by="rule",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Index drift"
            assert any("drift" in d for d in report.drift)

            # a newer FAILED build → Build failed outranks everything
            await conn.execute(
                tables.builds.insert().values(
                    project=project, status="failed", started_at=datetime.now(tz=UTC)
                )
            )
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Build failed"
            assert report.metrics["last_failed_build"] is not None
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


async def test_eval_regression_light_needs_comparable_reports(project: str) -> None:
    """§20's light fires only on MEASURED, same-fingerprint reports — an
    unscored or different-suite ready build never lights it (the gate fails
    closed elsewhere; the light must not guess)."""
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            await conn.execute(
                tables.builds.insert().values(
                    project=project,
                    status="active",
                    started_at=NOW,
                    eval={"score": 0.9, "failed": 0, "fingerprint": "fp"},
                )
            )
            # ready build, regressing score, SAME fingerprint → light on
            await conn.execute(
                tables.builds.insert().values(
                    project=project,
                    status="ready",
                    started_at=datetime.now(tz=UTC),
                    eval={"score": 0.5, "failed": 0, "fingerprint": "fp"},
                )
            )
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Eval regression"

            # different fingerprint → incomparable → the light goes dark
            await conn.execute(
                tables.builds.update()
                .where(tables.builds.c.project == project, tables.builds.c.status == "ready")
                .values(eval={"score": 0.5, "failed": 0, "fingerprint": "other"})
            )
            await conn.commit()
            report = await health_report(conn, qdrant, session, project)
            assert report.status == "Healthy"
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()
