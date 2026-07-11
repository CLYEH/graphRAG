"""Why (BA7): the observability routers must run LIVE — /health's drift light
probes the API's OWN lazy Neo4j/Qdrant clients end-to-end (the wiring is the
task; core's light semantics are test_observability_health's), /metrics
serves the same producer's numbers, and /eval's §20 predicates (latest
report, gate-passed boolean, fingerprint-comparable regression) run on real
builds rows. The savepoint harness cannot see the endpoints' own connections,
so fixtures COMMIT and clean up (the query-integration pattern).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from core.config import get_settings
from core.registry import create_project
from core.stores.tables import builds, entities, projects

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def test_health_metrics_eval_end_to_end(migrated: None) -> None:
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)

        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://t") as client,
        ):
            # bootstrap: no builds — a legitimate report, never a 409
            r = await client.get(f"/projects/{project}/health")
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["status"] == "healthy" and data["active_build_id"] is None
            assert r.json()["meta"]["build_id"] is None

            r = await client.get(f"/projects/{project}/eval")
            assert r.status_code == 200
            assert r.json()["data"] == {
                "build_id": None,
                "passed": None,
                "regression": None,
                "metrics": {},
            }

            r = await client.get(f"/projects/{project}/metrics")
            assert r.status_code == 200
            assert r.json()["data"]["builds_total"] == 0

            r = await client.get(f"/projects/ghost-{uuid.uuid4().hex[:6]}/health")
            assert r.status_code == 404

            # drift, live: one active PG entity, nothing projected — the
            # router's own lazy Neo4j/Qdrant clients must carry the probe
            async with engine.connect() as conn, conn.begin():
                active_id = (
                    await conn.execute(
                        builds.insert()
                        .values(project=project, status="active", started_at=NOW)
                        .returning(builds.c.id)
                    )
                ).scalar_one()
                await conn.execute(
                    entities.insert().values(
                        project=project,
                        build_id=active_id,
                        type="Hall",
                        canonical_name="Main Hall",
                        entity_key=f"fpv1:hall|main-{uuid.uuid4().hex[:6]}",
                        status="active",
                    )
                )

            r = await client.get(f"/projects/{project}/health")
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["status"] == "index_drift"
            assert data["drift"]["failures"]  # pg=1 vs neo4j=0, measured live
            assert data["counts"]["entities"] == 1
            assert r.json()["meta"]["build_id"] == str(active_id)

            # /metrics: the SAME producer's numbers (class 5, live)
            r = await client.get(f"/projects/{project}/metrics")
            assert r.status_code == 200
            assert r.json()["data"]["entities"] == 1
            assert r.json()["data"]["builds_total"] == 1

            # eval, live §20 predicates: active scored 0.8, a NEWER ready
            # build scored 0.5 on the SAME fingerprint → served report is the
            # ready build's, gate-passed (failed=0), regression measured true
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    builds.update()
                    .where(builds.c.id == active_id)
                    .values(eval={"score": 0.8, "passed": 3, "failed": 0, "fingerprint": "fp"})
                )
                ready_id = (
                    await conn.execute(
                        builds.insert()
                        .values(
                            project=project,
                            status="ready",
                            started_at=NOW + timedelta(minutes=1),
                            eval={
                                "score": 0.5,
                                "passed": 3,
                                "failed": 0,
                                "fingerprint": "fp",
                                "metrics": {"groundedness": 0.7},
                            },
                        )
                        .returning(builds.c.id)
                    )
                ).scalar_one()

            r = await client.get(f"/projects/{project}/eval")
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["build_id"] == str(ready_id)
            assert data["passed"] is True and data["regression"] is True
            assert data["metrics"] == {"groundedness": 0.7}
            assert r.json()["meta"]["build_id"] == str(ready_id)

            # incomparable fingerprints → regression is NULL (measured facts
            # only), and failed>0 → the gate's own predicate says not passed
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    builds.update()
                    .where(builds.c.id == ready_id)
                    .values(eval={"score": 0.5, "passed": 1, "failed": 2, "fingerprint": "fp2"})
                )
            r = await client.get(f"/projects/{project}/eval")
            data = r.json()["data"]
            assert data["passed"] is False and data["regression"] is None

            # bool subclasses int (Codex #62): a malformed {"failed": false}
            # must read as NULL, never a passing report — and a boolean score
            # is UNSCORED, never 1.0 in the regression comparison.
            # Discriminating: the old isinstance checks returned passed=true.
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    builds.update()
                    .where(builds.c.id == ready_id)
                    .values(eval={"score": True, "failed": False, "fingerprint": "fp"})
                )
            r = await client.get(f"/projects/{project}/eval")
            data = r.json()["data"]
            assert data["passed"] is None and data["regression"] is None
    finally:
        async with engine.connect() as cleanup, cleanup.begin():
            await cleanup.execute(entities.delete().where(entities.c.project == project))
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.execute(projects.delete().where(projects.c.name == project))
        await engine.dispose()
