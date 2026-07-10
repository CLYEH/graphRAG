"""Why: BA6a's value is that the playground answers EXACTLY as the MCP tools
do — one binding/deadline/degradation machinery. The global mode is the pure-
Postgres member of the trio, so it proves the whole REST seam end-to-end on
live SQL (registry policy → per-request context → run_bounded_query → §16
reprojection) with the model FACTORIES faked (the #37 lesson — global never
calls them, but the shared ProjectContext constructs all clients; CI has no
key). Savepoint harness for the API conn; the query path binds its own
connection off the app engine, so fixtures COMMIT and clean up (lease-test
pattern).
"""

from __future__ import annotations

import uuid
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
from core.stores.tables import builds, community_reports, entities, projects

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_QUERY_POLICY: dict[str, Any] = {
    "schema_version": "1.0",
    "default_mode": "hybrid",
    "max_top_k": 20,
    "max_graph_hops": 3,
    "max_sql_rows": 100,
    "max_latency_ms": 10000,
    "require_sources": True,
    "expose_debug": True,
    "text_to_sql": {
        "enabled": False,
        "readonly": True,
        "allowed_tables": [],
        "blocked_keywords": ["insert", "update", "delete", "drop", "alter", "truncate"],
        "max_rows": 100,
        "timeout_ms": 5000,
    },
    "text_to_cypher": {
        "enabled": False,
        "readonly": True,
        "allowed_clauses": ["MATCH", "WHERE", "RETURN", "LIMIT"],
        "blocked": ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
        "max_rows": 100,
        "timeout_ms": 5000,
    },
}


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def test_global_query_end_to_end_over_the_shared_seam(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the shared ProjectContext constructs every client; global calls none of
    # the models, but construction must not demand a key (#37 factory fakes)
    monkeypatch.setattr("api.deps.embedding_model", lambda: object())
    monkeypatch.setattr("api.deps.chat_model", lambda: object())

    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project, config={"query_policy": _QUERY_POLICY})
            build_id = (
                await conn.execute(
                    builds.insert().values(project=project, status="active").returning(builds.c.id)
                )
            ).scalar_one()
            entity_id = (
                await conn.execute(
                    entities.insert()
                    .values(
                        project=project,
                        build_id=build_id,
                        type="Hall",
                        canonical_name="Main Hall",
                        entity_key=f"fpv1:hall|main-{uuid.uuid4().hex[:6]}",
                        status="active",
                    )
                    .returning(entities.c.id)
                )
            ).scalar_one()
            await conn.execute(
                community_reports.insert().values(
                    project=project,
                    build_id=build_id,
                    level=0,
                    title="Main Hall cluster",
                    summary="Exhibits around the main hall.",
                    member_entity_ids=[entity_id],
                    rating=0.9,
                )
            )

        app = create_app()
        transport = ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),  # the query path binds off app.state.engine
            AsyncClient(transport=transport, base_url="http://t") as client,
        ):
            r = await client.post(
                f"/projects/{project}/query/global", json={"query": "main hall overview"}
            )
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["mode"] == "global"
            assert data["build_id"] == str(build_id)
            assert r.json()["meta"]["build_id"] == str(build_id)
            assert data["warnings"] == [] or all("code" in w for w in data["warnings"])
            (result,) = data["results"]
            assert result["result_type"] == "community_report"
            # #35 grounding, explicitly: the emitted report cites its REAL
            # member entity (ungrounded ids would have dropped the report)
            assert str(entity_id) in [ref["id"] for ref in result["source_refs"]]
            assert "main hall" in result["text"].lower() or "Main Hall" in result["title"]

            # the policy gate is live on this surface too
            r = await client.post(f"/projects/{project}/query/global", json={"query": ""})
            assert r.status_code == 400
    finally:
        async with engine.connect() as cleanup, cleanup.begin():
            await cleanup.execute(
                community_reports.delete().where(community_reports.c.project == project)
            )
            await cleanup.execute(entities.delete().where(entities.c.project == project))
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.execute(projects.delete().where(projects.c.name == project))
        await engine.dispose()
