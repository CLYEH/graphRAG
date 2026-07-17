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
from core.stores.tables import (
    builds,
    chunks,
    documents,
    entities,
    entity_mentions,
    relation_evidence,
    relations,
)

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
        # ASGITransport does not drive lifespan — enter it explicitly so
        # app.state (the subgraph endpoint's lazy Neo4j driver slot) exists
        # and the driver is closed on exit (the BA2e-2 SSE lesson)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://t") as client,
        ):
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


async def _make_entity(
    conn: AsyncConnection, project: str, build_id: uuid.UUID, name: str, **over: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "project": project,
        "build_id": build_id,
        "type": "Person",
        "canonical_name": name,
        "entity_key": f"fpv1:person|{name.lower()}-{uuid.uuid4().hex[:6]}",
        "status": "active",
    }
    values.update(over)
    return cast(
        "uuid.UUID",
        (
            await conn.execute(entities.insert().values(**values).returning(entities.c.id))
        ).scalar_one(),
    )


async def _make_relation(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID,
    src: uuid.UUID,
    dst: uuid.UUID,
    **over: Any,
) -> uuid.UUID:
    values: dict[str, Any] = {
        "project": project,
        "build_id": build_id,
        "src_entity_id": src,
        "dst_entity_id": dst,
        "type": "WORKS_AT",
        "status": "active",
    }
    values.update(over)
    return cast(
        "uuid.UUID",
        (
            await conn.execute(relations.insert().values(**values).returning(relations.c.id))
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


async def test_entities_and_relations_scoped_with_evidence_on_detail(api: Api) -> None:
    # WHY (DR-006 + §27.4): the entity/relation surface has the same
    # invisibility guarantee as documents, and relation detail carries the
    # evidence audit trail — including evidence whose chunk was pruned (the
    # denormalized quote/source_uri survive; chunk_id may dangle by design).
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        archived = await _make_build(conn, project, "archived")
        alice = await _make_entity(conn, project, active, "Alice", created_by="llm")
        acme = await _make_entity(conn, project, active, "Acme")
        ghost = await _make_entity(conn, project, archived, "Ghost")
        rel = await _make_relation(
            conn,
            project,
            active,
            alice,
            acme,
            relation_signature="fpv1:alice|works_at|acme",
            created_by="llm",
            confidence=0.9,
        )
        await conn.execute(
            relation_evidence.insert().values(
                relation_id=rel,
                build_id=active,
                evidence_type="chunk",
                evidence_ref="doc-h:0",
                evidence_hash=f"eh-{uuid.uuid4().hex}",  # §27.4 dedup key, NOT NULL
                chunk_id=uuid.uuid4(),  # dangles by design (§27.4 prune survival)
                start_offset=0,
                end_offset=12,
                quote="Alice works.",
                source_uri="file:///d.txt",
                confidence=0.8,
            )
        )

    r = await client.get(f"/projects/{project}/entities")
    ids = {e["id"] for e in r.json()["data"]}
    assert ids == {str(alice), str(acme)}  # archived-build entity invisible
    assert r.json()["meta"]["build_id"] == str(active)
    got_alice = next(e for e in r.json()["data"] if e["id"] == str(alice))
    assert got_alice["created_by"] == "llm"
    got_acme = next(e for e in r.json()["data"] if e["id"] == str(acme))
    assert "created_by" not in got_acme  # NULL column → key omitted
    assert (await client.get(f"/projects/{project}/entities/{ghost}")).status_code == 404

    r = await client.get(f"/projects/{project}/relations")
    (listed,) = r.json()["data"]
    assert listed["id"] == str(rel)
    assert "evidence" not in listed  # detail-only

    r = await client.get(f"/projects/{project}/relations/{rel}")
    detail = r.json()["data"]
    assert detail["relation_signature"] == "fpv1:alice|works_at|acme"
    (ev,) = detail["evidence"]
    assert ev["quote"] == "Alice works." and ev["source_uri"] == "file:///d.txt"
    assert ev["evidence_ref"] == "doc-h:0"


async def test_entities_paginate_by_id_desc(api: Api) -> None:
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        ids = [await _make_entity(conn, project, active, f"E{i}") for i in range(3)]

    expected = [str(i) for i in sorted(ids, reverse=True)]
    r1 = await client.get(f"/projects/{project}/entities", params={"limit": 2})
    token = r1.json()["meta"]["next_cursor"]
    assert [e["id"] for e in r1.json()["data"]] == expected[:2] and token
    r2 = await client.get(f"/projects/{project}/entities", params={"limit": 2, "cursor": token})
    assert [e["id"] for e in r2.json()["data"]] == expected[2:]
    assert r2.json()["meta"]["next_cursor"] is None


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


async def test_subgraph_end_to_end_over_registry_policy(api: Api) -> None:
    # WHY: BA3c's seam is everything AROUND the traversal (the traversal
    # itself is C6c's, integration-proven in test_query_graph_integration):
    # policy read from the REGISTRY config and frozen-schema validated (owner
    # decision 2026-07-10 — strict, no invented §21 defaults), the binding,
    # a REAL Neo4j session through the lazy driver seam, and the envelope.
    # The graph projection is empty for this build, so the context is exactly
    # the mention-backed seed with no edges — SoR-only emission, live.
    client, conn = api
    project = await _make_project(client)
    r = await client.patch(f"/projects/{project}", json={"config": {"query_policy": _QUERY_POLICY}})
    assert r.status_code == 200
    async with conn.begin_nested():
        active = await _make_build(conn, project, "active")
        seed = await _make_entity(conn, project, active, "Hall-A")
        await conn.execute(
            entity_mentions.insert().values(
                entity_id=seed, source_kind="structured", source_ref="halls:1"
            )
        )

    r = await client.get(f"/projects/{project}/graph/subgraph", params={"entity_id": str(seed)})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["edges"] == []
    (node,) = data["nodes"]
    assert node["id"] == str(seed) and node["label"] == "Hall-A" and node["type"] == "Person"
    assert r.json()["meta"]["build_id"] == str(active)

    # policy gates live too: over-ceiling hops rejected, unconfigured project loud
    r = await client.get(
        f"/projects/{project}/graph/subgraph",
        params={"entity_id": str(seed), "hops": 9},
    )
    assert r.status_code == 400 and r.json()["error"]["details"]["max_graph_hops"] == 3
    bare = await _make_project(client)
    async with conn.begin_nested():
        await _make_build(conn, bare, "active")
    r = await client.get(f"/projects/{bare}/graph/subgraph", params={"entity_id": str(seed)})
    assert r.status_code == 400 and r.json()["error"]["details"] == {"query_policy": "missing"}


async def test_ss1a_facets_filter_server_side(api: Api) -> None:
    """SS1a (owner-approved slice of SS1): "load more until you find it"
    doesn't scale — the list endpoints must filter SERVER-side. Mixed rows in
    one build; each facet returns exactly its matches (never the whole page),
    combined facets AND together, and a facet naming nothing returns empty —
    proof the WHERE reached SQL, not a client-side sieve."""
    client, conn = api
    project = await _make_project(client)
    async with conn.begin_nested():
        build_id = await _make_build(conn, project, "active")
        hall = await _make_entity(conn, project, build_id, "Hall", type="EXHIBIT")
        await _make_entity(conn, project, build_id, "Alice", type="Person")
        await _make_entity(conn, project, build_id, "Ghost", type="Person", status="rejected")

    r = await client.get(f"/projects/{project}/entities?filter[type]=EXHIBIT")
    assert r.status_code == 200
    assert [row["id"] for row in r.json()["data"]] == [str(hall)]

    r = await client.get(f"/projects/{project}/entities?filter[status]=rejected")
    assert [row["canonical_name"] for row in r.json()["data"]] == ["Ghost"]

    # combined facets AND: Person ∩ rejected = Ghost only
    r = await client.get(
        f"/projects/{project}/entities?filter[type]=Person&filter[status]=rejected"
    )
    assert [row["canonical_name"] for row in r.json()["data"]] == ["Ghost"]

    # a facet naming nothing → empty list, never the unfiltered page
    r = await client.get(f"/projects/{project}/entities?filter[type]=Spaceship")
    assert r.status_code == 200 and r.json()["data"] == []
