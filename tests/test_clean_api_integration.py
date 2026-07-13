"""Why: the preview's two live claims can only be proven against real
Postgres — that the document source reads the raw text THROUGH the DR-006
build-scoped repo (a document parked in a non-active build must be invisible,
404, even though its row exists), and that the endpoint's defining promise
holds end-to-end: after a preview the chunks table contains exactly the rows
it contained before (DR-009: pure function, nothing persisted). Savepoint-
per-request harness (BA1b pattern); nothing lands in the dev DB.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

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
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://t") as client,
        ):
            yield client, conn
    finally:
        await outer.rollback()
        await conn.close()
        await engine.dispose()


async def _make_project(client: AsyncClient, config: dict[str, Any] | None = None) -> str:
    name = f"itest-{uuid.uuid4().hex[:10]}"
    body: dict[str, Any] = {"name": name}
    if config is not None:
        body["config"] = config
    assert (await client.post("/projects", json=body)).status_code == 201
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
    conn: AsyncConnection, project: str, build_id: uuid.UUID, raw: str
) -> uuid.UUID:
    return cast(
        "uuid.UUID",
        (
            await conn.execute(
                documents.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    source_uri="file:///d.txt",
                    raw=raw,
                    content_hash=f"h-{uuid.uuid4().hex[:8]}",
                    mime="text/plain",
                    status="ingested",
                )
                .returning(documents.c.id)
            )
        ).scalar_one(),
    )


async def _chunk_rows(conn: AsyncConnection) -> int:
    count = (await conn.execute(sa.select(sa.func.count()).select_from(chunks))).scalar_one()
    return int(count)


@pytest.mark.anyio
async def test_document_preview_reads_the_active_build_and_persists_nothing(api: Api) -> None:
    client, conn = api
    project = await _make_project(client)
    active = await _make_build(conn, project, "active")
    raw = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 20
    doc = await _make_document(conn, project, active, raw)
    before = await _chunk_rows(conn)

    r = await client.post(
        f"/projects/{project}/clean/preview",
        json={"document_id": str(doc), "max_chars": 120, "overlap": 20},
    )

    assert r.status_code == 200
    payload = r.json()
    # served by the ACTIVE build, and the offsets point into the stored raw
    assert payload["meta"]["build_id"] == str(active)
    got = payload["data"]["chunks"]
    assert len(got) > 1
    for c in got:
        assert raw[c["start_offset"] : c["end_offset"]] == c["text"]
    # the defining promise: a preview writes NOTHING (chunk count unchanged)
    assert await _chunk_rows(conn) == before


@pytest.mark.anyio
async def test_document_in_a_non_active_build_is_invisible(api: Api) -> None:
    # The row exists — in a build that is not active. DR-006 means the preview
    # must not be able to see it: previewing against superseded corpus would
    # report chunk shapes for text the platform no longer serves.
    client, conn = api
    project = await _make_project(client)
    await _make_build(conn, project, "active")
    parked = await _make_build(conn, project, "ready")
    hidden = await _make_document(conn, project, parked, "hidden text " * 50)

    r = await client.post(f"/projects/{project}/clean/preview", json={"document_id": str(hidden)})

    assert r.status_code == 404


@pytest.mark.anyio
async def test_text_preview_uses_the_project_config_from_the_registry(api: Api) -> None:
    # End-to-end fallback proof: the config travels client → registry →
    # preview. 30-char windows must split this text; the engine default (1200)
    # would return one chunk — so >1 chunks proves the PROJECT's values were
    # read back out of Postgres, not the module constants.
    client, conn = api
    project = await _make_project(client, config={"chunking": {"max_chars": 30, "overlap": 5}})
    before = await _chunk_rows(conn)

    r = await client.post(
        f"/projects/{project}/clean/preview",
        json={"text": "one two three four five six seven eight nine ten eleven twelve"},
    )

    assert r.status_code == 200
    payload = r.json()
    assert payload["meta"]["build_id"] is None  # no build involved — and none required
    assert len(payload["data"]["chunks"]) > 1
    assert await _chunk_rows(conn) == before
