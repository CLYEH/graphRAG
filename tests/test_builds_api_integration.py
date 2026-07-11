"""Why (BA8): the builds facade must run LIVE — activation preflight (drift
over real Neo4j/Qdrant, the §20 gate on real eval blocks), the atomic
promotion + §27 idempotency composition (reservation and promotion in ONE
request transaction: the replay survives the state change it caused), the
TARGETED rollback with its archived-only gate exemption (both sides
discriminating: an incomparable-history archived target succeeds ONLY under
the exemption, and a regressing READY target through /rollback still 409s),
and the list/get pagination over real rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from core.config import get_settings
from core.registry import create_project
from core.stores.tables import builds, idempotency_keys, projects

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _eval(score: float, fingerprint: str = "fp") -> dict[str, Any]:
    return {"score": score, "passed": 3, "failed": 0, "fingerprint": fingerprint}


async def test_builds_lifecycle_end_to_end(migrated: None) -> None:
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    app = create_app()
    transport = ASGITransport(app=app)

    async def _seed_build(minutes: int, **values: Any) -> uuid.UUID:
        async with engine.connect() as conn, conn.begin():
            row = await conn.execute(
                builds.insert()
                .values(project=project, started_at=NOW + timedelta(minutes=minutes), **values)
                .returning(builds.c.id)
            )
            build_id: uuid.UUID = row.scalar_one()
            return build_id

    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
        build_a = await _seed_build(0, status="ready", eval=_eval(0.8))

        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://t") as client,
        ):
            # first-ever activation: scored, failed==0, empty projections
            # agree (0=0=0) — the whole §14 preflight runs live
            key = f"k-{uuid.uuid4().hex[:8]}"
            r = await client.post(
                f"/projects/{project}/builds/{build_a}/activate",
                headers={"Idempotency-Key": key},
            )
            assert r.status_code == 200, r.text
            first_body = r.json()
            assert first_body["data"]["status"] == "active"
            assert first_body["meta"]["build_id"] == str(build_a)

            # §27 replay: the SAME key returns the stored 200 VERBATIM even
            # though re-executing would now 409 (build already active) — the
            # reservation committed atomically WITH the promotion
            r = await client.post(
                f"/projects/{project}/builds/{build_a}/activate",
                headers={"Idempotency-Key": key},
            )
            assert r.status_code == 200
            assert r.json() == first_body

            # a FRESH key re-executes: already-active → preflight 409, and
            # the failed attempt must not poison its key (BA1b: retry same
            # key re-runs, still 409 — never a stored-failure replay of 200)
            r = await client.post(
                f"/projects/{project}/builds/{build_a}/activate",
                headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}"},
            )
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "BUILD_NOT_READY"
            assert any("already active" in f for f in r.json()["error"]["details"]["failures"])

            # promote B (comparable, non-regressing) — A is displaced
            build_b = await _seed_build(1, status="ready", eval=_eval(0.78))
            r = await client.post(f"/projects/{project}/builds/{build_b}/activate")
            assert r.status_code == 200, r.text
            r = await client.get(f"/projects/{project}/builds/{build_a}")
            assert r.json()["data"]["status"] == "archived"

            # make A's history INCOMPARABLE (old fingerprint): the gated path
            # would 409 on it — rollback succeeding proves the archived-target
            # exemption discriminatingly
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    builds.update()
                    .where(builds.c.id == build_a)
                    .values(eval=_eval(0.8, fingerprint="fp-old"))
                )
            r = await client.post(f"/projects/{project}/builds/{build_a}/rollback")
            assert r.status_code == 200, r.text
            assert r.json()["data"]["status"] == "active"
            r = await client.get(f"/projects/{project}/builds/{build_b}")
            assert r.json()["data"]["status"] == "archived"

            # a READY target through /rollback keeps the §20 gate: C's
            # fingerprint matches nothing active (A is fp-old) → incomparable
            # → fail-closed 409, proving /rollback is no gate bypass
            build_c = await _seed_build(2, status="ready", eval=_eval(0.5))
            r = await client.post(f"/projects/{project}/builds/{build_c}/rollback")
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "BUILD_NOT_READY"

            # list: id-desc keyset over the three builds, opaque cursor
            r = await client.get(f"/projects/{project}/builds", params={"limit": 2})
            assert r.status_code == 200
            page_one = r.json()["data"]
            assert len(page_one) == 2 and r.json()["meta"]["next_cursor"]
            r = await client.get(
                f"/projects/{project}/builds",
                params={"limit": 2, "cursor": r.json()["meta"]["next_cursor"]},
            )
            page_two = r.json()["data"]
            assert len(page_two) == 1 and r.json()["meta"]["next_cursor"] is None
            ids = {b["id"] for b in page_one + page_two}
            assert ids == {str(build_a), str(build_b), str(build_c)}

            # unknown build → 404 BUILD_NOT_FOUND (never a 409 saying "not found")
            r = await client.post(f"/projects/{project}/builds/{uuid.uuid4()}/activate")
            assert (r.status_code, r.json()["error"]["code"]) == (404, "BUILD_NOT_FOUND")
    finally:
        async with engine.connect() as cleanup, cleanup.begin():
            await cleanup.execute(
                idempotency_keys.delete().where(idempotency_keys.c.project == project)
            )
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.execute(projects.delete().where(projects.c.name == project))
        await engine.dispose()
