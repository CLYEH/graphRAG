"""Why: BA5 is Console v1's review workflow end-to-end — the ACTIVE-build
scoping of the queue (DR-006 invisibility), the decide→ledger→audit flow over
live SQL, the §27 idempotent replay NOT double-writing the ledger (a retried
approve must not stack carry-forward entries), and the §17 refusal shapes.
Savepoint-per-request harness; nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.deps import db_conn
from core.config import get_settings
from core.stores.tables import builds, entities, merge_candidates, review_ledger

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

Api = tuple[AsyncClient, AsyncConnection]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


@pytest.fixture()
async def api(migrated: None) -> AsyncIterator[Api]:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(dsn, poolclass=NullPool)
    conn = await engine.connect()
    outer = await conn.begin()
    app = create_app()

    async def _override() -> AsyncIterator[AsyncConnection]:
        async with conn.begin_nested():
            yield conn

    app.dependency_overrides[db_conn] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, conn
    finally:
        await outer.rollback()
        await conn.close()
        await engine.dispose()


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def _seed(
    client: AsyncClient, conn: AsyncConnection, *, build_status: str = "active"
) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Project (via API) + build + entity pair + pending candidate.
    Returns (project, build_id, candidate_id)."""
    project = _proj()
    assert (await client.post("/projects", json={"name": project})).status_code == 201
    async with conn.begin_nested():
        build_id = (
            await conn.execute(
                builds.insert().values(project=project, status=build_status).returning(builds.c.id)
            )
        ).scalar_one()
        ids = []
        for name in ("Alice", "Alyce"):
            ids.append(
                (
                    await conn.execute(
                        entities.insert()
                        .values(
                            project=project,
                            build_id=build_id,
                            type="Person",
                            canonical_name=name,
                            entity_key=f"fpv1:person|{name.lower()}-{uuid.uuid4().hex[:6]}",
                            status="active",
                        )
                        .returning(entities.c.id)
                    )
                ).scalar_one()
            )
        candidate_id = (
            await conn.execute(
                merge_candidates.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    left_entity_id=ids[0],
                    right_entity_id=ids[1],
                    score=0.85,
                )
                .returning(merge_candidates.c.id)
            )
        ).scalar_one()
    return project, build_id, candidate_id


async def test_review_flow_list_approve_refuse(api: Api) -> None:
    client, conn = api
    project, build_id, candidate_id = await _seed(client, conn)

    r = await client.get(f"/projects/{project}/merge-candidates")
    assert r.status_code == 200
    (candidate,) = r.json()["data"]
    assert candidate["id"] == str(candidate_id)
    assert candidate["status"] == "pending"
    assert candidate["features"] == {}  # optional non-nullable object: NULL → {}
    assert candidate["impact"] is None  # contract-nullable: null stays null
    assert candidate["decision"] is None
    assert r.json()["meta"]["build_id"] == str(build_id)

    r = await client.post(
        f"/projects/{project}/merge-candidates/{candidate_id}/approve",
        json={"reason": "same person"},
    )
    assert r.status_code == 200
    decided = r.json()["data"]
    assert decided["status"] == "approved" and decided["decision"] == "approve"
    assert decided["decided_by"] == "console" and decided["reason"] == "same person"
    assert decided["decided_at"] is not None

    ledger_rows = (
        await conn.execute(
            sa.select(review_ledger.c.decision, review_ledger.c.reason).where(
                review_ledger.c.project == project
            )
        )
    ).all()
    assert [(row.decision, row.reason) for row in ledger_rows] == [("approve", "same person")]

    # §17: approved is terminal — a second verb is a machine-readable 400
    r = await client.post(f"/projects/{project}/merge-candidates/{candidate_id}/reject")
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"status": "approved", "decision": "reject"}

    # and the decided candidate leaves the queue — the list matches §19's
    # pending_review definition (pending+deferred only), never re-serving
    # handled work (Codex #59 R1)
    r = await client.get(f"/projects/{project}/merge-candidates")
    assert r.json()["data"] == []


async def test_status_filter_serves_audit_and_fails_loud(api: Api) -> None:
    """GOV4/GAPS O4: the contract's Filter param must never silently no-op —
    the adaptation was once misled by a 200 + pending rows into believing
    `filter=status:approved` had taken effect. filter[status] is the audit
    surface over the same SoR (decided rows leave the default queue but stay
    retrievable when named); every unsupported spelling is a loud 400."""
    client, conn = api
    project, _build_id, candidate_id = await _seed(client, conn)
    r = await client.post(f"/projects/{project}/merge-candidates/{candidate_id}/approve")
    assert r.status_code == 200

    # the audit facet: a decided row is listable when its status is named
    r = await client.get(f"/projects/{project}/merge-candidates?filter[status]=approved")
    assert r.status_code == 200
    (row,) = r.json()["data"]
    assert row["id"] == str(candidate_id) and row["status"] == "approved"

    # a named status the row does NOT have → empty, never the default queue
    r = await client.get(f"/projects/{project}/merge-candidates?filter[status]=deferred")
    assert r.status_code == 200 and r.json()["data"] == []

    # fail loud, never pretend: a value outside §17's vocabulary, a field the
    # endpoint doesn't implement, the bare non-deepObject spelling (O4's
    # exact evidence — it used to slip past the `filter[` prefix check), and
    # an ambiguous repeated param (拒絕勝於默選一邊)
    for qs in (
        "filter[status]=bogus",
        "filter[score]=1",
        "filter=status:approved",
        "filter[status]=approved&filter[status]=rejected",
    ):
        r = await client.get(f"/projects/{project}/merge-candidates?{qs}")
        assert r.status_code == 400, qs
        assert r.json()["error"]["code"] == "VALIDATION_ERROR", qs


async def test_idempotent_replay_never_stacks_ledger_entries(api: Api) -> None:
    # WHY §27: a client retrying its approve with the same key must get the
    # stored response back — NOT a second carry-forward ledger entry (stacked
    # entries would be silent history rewrites), and NOT the 400 the §17
    # machine would give a fresh duplicate approve.
    client, conn = api
    project, _, candidate_id = await _seed(client, conn)
    key = f"k-{uuid.uuid4().hex[:8]}"

    r1 = await client.post(
        f"/projects/{project}/merge-candidates/{candidate_id}/defer",
        json={},
        headers={"Idempotency-Key": key},
    )
    r2 = await client.post(
        f"/projects/{project}/merge-candidates/{candidate_id}/defer",
        json={},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["data"] == r2.json()["data"]  # replayed verbatim
    count = (
        await conn.execute(
            sa.select(sa.func.count())
            .select_from(review_ledger)
            .where(review_ledger.c.project == project)
        )
    ).scalar_one()
    assert count == 1  # ONE defer entry, not two


async def test_queue_is_scoped_to_the_active_build(api: Api) -> None:
    client, conn = api
    project, _, _ = await _seed(client, conn)
    other_project, _, foreign_candidate = await _seed(client, conn, build_status="archived")

    # the archived build's project has NO active build → the queue is a 409,
    # and its candidate is unreachable through this surface
    r = await client.get(f"/projects/{other_project}/merge-candidates")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"
    r = await client.post(f"/projects/{other_project}/merge-candidates/{foreign_candidate}/approve")
    assert r.status_code == 409

    # and one project's candidate is invisible through another's queue
    r = await client.get(f"/projects/{project}/merge-candidates")
    ids = {c["id"] for c in r.json()["data"]}
    assert str(foreign_candidate) not in ids


async def test_decide_edges_null_body_unknown_candidate(api: Api) -> None:
    client, conn = api
    project, _, candidate_id = await _seed(client, conn)

    r = await client.post(
        f"/projects/{project}/merge-candidates/{candidate_id}/approve",
        content=b"null",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400  # explicit null body (the #53 R5 class)

    r = await client.post(f"/projects/{project}/merge-candidates/{uuid.uuid4()}/approve")
    assert r.status_code == 404  # unknown candidate: true 404, coarse code
