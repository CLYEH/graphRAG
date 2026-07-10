"""Why: BA3a is the API's first consumer of the ACTIVE build, and its whole
value is the DR-006 guarantee — a client can NEVER see another build's (or
another project's) rows through these endpoints, and meta.build_id names
exactly the build that served the response. That scoping, the live keyset
pagination (order + cursor walk against real SQL), the raw-on-detail-only
key, and the no-active-build 409 must hold end-to-end against Postgres —
fakes can't prove the injected WHERE. Savepoint-per-request harness (BA1b
pattern); nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.deps import db_conn
from core.config import get_settings
from core.stores.tables import builds, chunks, documents

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


async def _make_project(client: AsyncClient) -> str:
    name = _proj()
    assert (await client.post("/projects", json={"name": name})).status_code == 201
    return name


async def _make_build(conn: AsyncConnection, project: str, status: str) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                builds.insert().values(project=project, status=status).returning(builds.c.id)
            )
        ).scalar_one(),
    )


async def _make_document(
    conn: AsyncConnection, project: str, build_id: uuid.UUID, **over: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "project": project,
        "build_id": build_id,
        "source_uri": "file:///d.txt",
        "raw": "the raw text",
        "content_hash": f"h-{uuid.uuid4().hex[:8]}",
        "mime": "text/plain",
        "status": "ingested",
    }
    values.update(over)
    return cast(
        "uuid.UUID",
        (
            await conn.execute(documents.insert().values(**values).returning(documents.c.id))
        ).scalar_one(),
    )


async def _make_chunk(
    conn: AsyncConnection, document_id: uuid.UUID, build_id: uuid.UUID, ordinal: int
) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                chunks.insert()
                .values(
                    document_id=document_id,
                    build_id=build_id,
                    ordinal=ordinal,
                    text=f"chunk {ordinal}",
                    start_offset=0,
                    end_offset=7,
                )
                .returning(chunks.c.id)
            )
        ).scalar_one(),
    )


async def test_inspection_is_scoped_to_the_active_build_only(api: Api) -> None:
    # WHY (DR-006): the endpoints must be structurally unable to leak another
    # build's or another project's rows — the exact "never mix old-version
    # data" guarantee the repo layer exists for.
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        archived = await _make_build(conn, project, "archived")
        visible = await _make_document(conn, project, active)
        stale = await _make_document(conn, project, archived)
        # another project's ACTIVE world must be invisible too
        other = _proj()
        assert (await client.post("/projects", json={"name": other})).status_code == 201
        other_active = await _make_build(conn, other, "active")
        foreign = await _make_document(conn, other, other_active)

    r = await client.get(f"/projects/{project}/documents")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["data"]}
    assert ids == {str(visible)}
    assert r.json()["meta"]["build_id"] == str(active)  # names the serving build

    # the archived build's document is a 404 through the detail GET as well
    assert (await client.get(f"/projects/{project}/documents/{stale}")).status_code == 404
    assert (await client.get(f"/projects/{project}/documents/{foreign}")).status_code == 404


async def test_documents_paginate_by_id_desc_with_opaque_cursor(api: Api) -> None:
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        doc_ids = [await _make_document(conn, project, active) for _ in range(3)]

    expected = [str(i) for i in sorted(doc_ids, reverse=True)]  # id desc
    r1 = await client.get(f"/projects/{project}/documents", params={"limit": 2})
    page1 = [d["id"] for d in r1.json()["data"]]
    token = r1.json()["meta"]["next_cursor"]
    assert page1 == expected[:2] and token

    r2 = await client.get(f"/projects/{project}/documents", params={"limit": 2, "cursor": token})
    page2 = [d["id"] for d in r2.json()["data"]]
    assert page2 == expected[2:]
    assert r2.json()["meta"]["next_cursor"] is None  # last page says so
    for doc in r1.json()["data"] + r2.json()["data"]:
        assert "raw" not in doc  # detail-only key


async def test_chunks_paginate_in_reading_order_across_documents(api: Api) -> None:
    # WHY: (document_id asc, ordinal asc) is a TOTAL order under the unique
    # constraint — the cursor walk must cross a document boundary without
    # skipping or repeating a row.
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        d1 = await _make_document(conn, project, active)
        d2 = await _make_document(conn, project, active)
        first_doc, second_doc = sorted([d1, d2])
        for doc in (first_doc, second_doc):
            for ordinal in range(2):
                await _make_chunk(conn, doc, active, ordinal)

    collected: list[tuple[str, int]] = []
    cursor: str | None = None
    for _ in range(3):  # 4 rows at limit 2 → exactly 2 pages, loop bounded
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"/projects/{project}/chunks", params=params)
        collected += [(c["document_id"], c["ordinal"]) for c in r.json()["data"]]
        cursor = r.json()["meta"]["next_cursor"]
        if cursor is None:
            break
    assert collected == [
        (str(first_doc), 0),
        (str(first_doc), 1),
        (str(second_doc), 0),
        (str(second_doc), 1),
    ]


async def test_chunk_detail_and_document_raw(api: Api) -> None:
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        doc = await _make_document(conn, project, active)
        chunk = await _make_chunk(conn, doc, active, 0)

    r = await client.get(f"/projects/{project}/documents/{doc}")
    assert r.status_code == 200
    assert r.json()["data"]["raw"] == "the raw text"  # detail carries raw

    r = await client.get(f"/projects/{project}/chunks/{chunk}")
    assert r.status_code == 200
    got = r.json()["data"]
    assert got["document_id"] == str(doc) and got["ordinal"] == 0
    assert got["metadata"] == {}  # DB NULL → the empty object, not null
    # the cleaning path writes chunks with no status (this fixture mirrors it):
    # the frozen Chunk.status is optional NON-nullable, so the key is absent
    assert "status" not in got
    r = await client.get(f"/projects/{project}/documents/{doc}")
    assert r.json()["data"]["status"] == "ingested"  # a real status rides along


async def test_no_active_build_is_409_and_missing_project_404(api: Api) -> None:
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        await _make_build(conn, project, "archived")  # builds exist, none active

    r = await client.get(f"/projects/{project}/documents")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"

    r = await client.get(f"/projects/{_proj()}/chunks")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"
