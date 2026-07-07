"""Why: BA1b is the HTTP contract the Console/SPA depends on, so its behaviors
must hold end-to-end against live Postgres — the §15 envelope, opaque-cursor
paging, the §27 idempotency replay/conflict/TTL, the 204-no-body delete, and the
domain→frozen-code error mappings (including the flagged no-conflict-code gap).
Each request runs in a savepoint inside one outer transaction that is rolled
back at teardown, so nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.deps import db_conn
from core.config import get_settings
from core.stores.tables import builds, idempotency_keys, jobs

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


@pytest.fixture()
async def api(migrated: None) -> AsyncIterator[tuple[AsyncClient, AsyncConnection]]:
    """A TestClient over create_app() whose db_conn is overridden to a single
    connection in an outer transaction; each request gets a savepoint (so a
    per-request commit/rollback is faithful) and the outer transaction is rolled
    back at teardown."""
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


def _assert_envelope(body: dict[str, Any], *, paginated: bool = False) -> None:
    assert set(body) == {"data", "meta"}
    meta = body["meta"]
    assert meta["build_id"] is None  # registry endpoints touch no build
    assert isinstance(meta["request_id"], str)
    assert "elapsed_ms" in meta
    if paginated:
        assert "next_cursor" in meta


async def test_project_crud_roundtrip(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, _ = api
    name = _proj()

    r = await client.post("/projects", json={"name": name, "display_name": "Demo"})
    assert r.status_code == 201
    _assert_envelope(r.json())
    assert r.json()["data"]["name"] == name
    assert r.headers["X-Request-ID"] == r.json()["meta"]["request_id"]

    r = await client.get(f"/projects/{name}")
    assert r.status_code == 200
    assert r.json()["data"]["display_name"] == "Demo"

    r = await client.get("/projects")
    assert r.status_code == 200
    _assert_envelope(r.json(), paginated=True)
    assert name in [p["name"] for p in r.json()["data"]]

    # PATCH: omitted display_name unchanged, description set
    r = await client.patch(f"/projects/{name}", json={"description": "desc"})
    assert r.status_code == 200
    assert r.json()["data"]["display_name"] == "Demo"  # untouched
    assert r.json()["data"]["description"] == "desc"

    r = await client.delete(f"/projects/{name}")
    assert r.status_code == 204
    assert r.content == b""  # no body
    assert r.headers["X-Request-ID"]
    assert (await client.get(f"/projects/{name}")).status_code == 404


async def test_source_crud_and_dto_shape(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, _ = api
    name = _proj()
    await client.post("/projects", json={"name": name})

    r = await client.post(f"/projects/{name}/sources", json={"uri": "file:///d", "kind": "file"})
    assert r.status_code == 201
    src = r.json()["data"]
    assert src["uri"] == "file:///d"
    assert src["kind"] == "file"
    assert "project" not in src  # contract Source carries no project

    r = await client.get(f"/projects/{name}/sources")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["data"]] == [src["id"]]


async def test_pagination_opaque_cursor(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, _ = api
    names = [_proj() for _ in range(3)]
    for n in names:
        await client.post("/projects", json={"name": n})

    r1 = await client.get("/projects", params={"limit": 2})
    assert len(r1.json()["data"]) == 2
    cursor = r1.json()["meta"]["next_cursor"]
    assert cursor  # more pages remain

    r2 = await client.get("/projects", params={"limit": 2, "cursor": cursor})
    seen = [p["name"] for p in r1.json()["data"] + r2.json()["data"]]
    assert set(names) <= set(seen)
    assert len(seen) == len(set(seen))  # no row repeated across pages


async def test_idempotency_replay_and_conflict(
    api: tuple[AsyncClient, AsyncConnection],
) -> None:
    client, conn = api
    name = _proj()
    headers = {"Idempotency-Key": uuid.uuid4().hex}

    r1 = await client.post("/projects", json={"name": name}, headers=headers)
    assert r1.status_code == 201

    # same key + same body → byte-identical replay, no second row
    r2 = await client.post("/projects", json={"name": name}, headers=headers)
    assert r2.status_code == 201
    assert r2.json() == r1.json()  # stored original response replayed verbatim

    # same key + different body → 409 IDEMPOTENCY_CONFLICT
    r3 = await client.post("/projects", json={"name": _proj()}, headers=headers)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_idempotency_expiry_reruns(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, conn = api
    name = _proj()
    key = uuid.uuid4().hex
    headers = {"Idempotency-Key": key}

    assert (await client.post("/projects", json={"name": name}, headers=headers)).status_code == 201
    # force the key past its TTL
    from sqlalchemy import text

    await conn.execute(
        text("UPDATE idempotency_keys SET expires_at = now() - interval '1 hour' WHERE key = :k"),
        {"k": key},
    )
    # expired → purged + re-run (not replay); the project now already exists, so
    # the re-run hits the duplicate path → 400, proving it did NOT replay the 201
    r = await client.post("/projects", json={"name": name}, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_idempotency_failed_write_leaves_no_poisoned_key(
    api: tuple[AsyncClient, AsyncConnection],
) -> None:
    """The property the whole reserve-first design rests on: a keyed write whose
    handler FAILS must roll its reservation back (no half-filled row), so a
    same-key retry RE-RUNS rather than replaying a status=NULL row (which would
    500 via int(None)). Depends on db_conn throwing the handler exception through
    its transaction — a refactor that broke that would surface here."""
    from sqlalchemy import func, select

    client, conn = api
    taken = _proj()
    await client.post("/projects", json={"name": taken})  # occupy the name
    key = uuid.uuid4().hex

    # keyed create of the taken name → produce() raises → 400, reservation rolled back
    r = await client.post("/projects", json={"name": taken}, headers={"Idempotency-Key": key})
    assert r.status_code == 400
    count = (
        await conn.execute(
            select(func.count()).select_from(idempotency_keys).where(idempotency_keys.c.key == key)
        )
    ).scalar_one()
    assert count == 0  # no poisoned row survived the failure

    # same key, now-valid body → re-runs (not a replay of a NULL-status row) → 201
    retry = await client.post("/projects", json={"name": _proj()}, headers={"Idempotency-Key": key})
    assert retry.status_code == 201


async def test_patch_null_config_is_400_not_500(
    api: tuple[AsyncClient, AsyncConnection],
) -> None:
    """A client sending config:null must get a 400 (config is non-nullable),
    never a NOT NULL IntegrityError surfaced as 500."""
    client, _ = api
    name = _proj()
    await client.post("/projects", json={"name": name})
    r = await client.patch(f"/projects/{name}", json={"config": None})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # create with null config is likewise a 400, not a coerced {}
    assert (
        await client.post("/projects", json={"name": _proj(), "config": None})
    ).status_code == 400


async def test_error_mappings(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, conn = api
    missing = _proj()

    assert (await client.get(f"/projects/{missing}")).status_code == 404
    assert (await client.patch(f"/projects/{missing}", json={})).status_code == 404
    assert (await client.delete(f"/projects/{missing}")).status_code == 404
    r = await client.post(f"/projects/{missing}/sources", json={"uri": "file:///x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    # duplicate create → the flagged no-conflict-code gap → VALIDATION_ERROR (400)
    name = _proj()
    await client.post("/projects", json={"name": name})
    dup = await client.post("/projects", json={"name": name})
    assert dup.status_code == 400
    assert dup.json()["error"]["code"] == "VALIDATION_ERROR"
    assert dup.json()["error"]["details"]["name"] == name

    # delete a project that has a build → refused, mapped to VALIDATION_ERROR
    await conn.execute(builds.insert().values(project=name, status="ready"))
    blocked = await client.delete(f"/projects/{name}")
    assert blocked.status_code == 400
    assert blocked.json()["error"]["details"]["builds"] == 1

    # delete a project with an active job → the new guard must map to 400, not
    # fall through to a 500
    jobless = _proj()
    await client.post("/projects", json={"name": jobless})
    await conn.execute(jobs.insert().values(project=jobless, kind="build", status="running"))
    blocked_jobs = await client.delete(f"/projects/{jobless}")
    assert blocked_jobs.status_code == 400
    assert blocked_jobs.json()["error"]["code"] == "VALIDATION_ERROR"
    assert blocked_jobs.json()["error"]["details"]["jobs"] == 1


async def test_request_validation(api: tuple[AsyncClient, AsyncConnection]) -> None:
    client, _ = api
    # limit out of bounds → 400 VALIDATION_ERROR (BA0's RequestValidation handler)
    assert (await client.get("/projects", params={"limit": 0})).status_code == 400
    assert (await client.get("/projects", params={"limit": 501})).status_code == 400
    # empty name → min_length rejects → 400
    assert (await client.post("/projects", json={"name": ""})).status_code == 400
    # malformed cursor → 400 VALIDATION_ERROR
    bad = await client.get("/projects", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    # unsupported sort → 400 (not silently ignored)
    assert (await client.get("/projects", params={"sort": "name:asc"})).status_code == 400


async def test_idempotency_row_persisted(api: tuple[AsyncClient, AsyncConnection]) -> None:
    """The reserve-first store actually records the key + replayable response."""
    from sqlalchemy import select

    client, conn = api
    key = uuid.uuid4().hex
    await client.post("/projects", json={"name": _proj()}, headers={"Idempotency-Key": key})
    row = (
        await conn.execute(
            select(idempotency_keys.c.status, idempotency_keys.c.endpoint).where(
                idempotency_keys.c.key == key
            )
        )
    ).one()
    assert row.status == 201
    assert row.endpoint == "createProject"
