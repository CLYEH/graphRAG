"""Why: UXC1b's whole promise is that document metadata captured at UPLOAD
survives the real pipeline — build ingest → ``documents.metadata`` → chunk→
document enrich on read → the exposure allowlist — and shows up (ONLY the
allowlisted slice) in a query's ``source_refs``, for ANY project's schema (no
meeting-specific path). This drives upload→build→eval→activate over live
Postgres + Qdrant with THREE UNRELATED document scenarios (a legal ruling, a
product spec, a research note) under one project schema, proving the path is
domain-agnostic; and it pins the exposure boundary — a ``governance`` field is
never leaked despite living in storage (DR-010 rule 7), and each document's own
context reaches its own chunk (no cross-document bleed).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
import yaml
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from api.app import create_app
from core.builds.lifecycle import activate
from core.builds.sources import resolve_source
from core.clean.chunking import clean_document
from core.config import get_settings
from core.eval.golden import load_golden
from core.eval.runner import run_eval
from core.index.indexing import index_build
from core.ingest.documents import ingest_documents
from core.mcp.policy import load_query_policy
from core.metadata.schema import load_metadata_exposure
from core.query.metadata_enrich import enrich_response_metadata
from core.query.semantic import semantic_search
from core.registry import create_project, list_sources
from core.stores.graph import BuildScopedGraphProjector, graph_driver
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.tables import builds, documents, sources
from core.stores.vectors import (
    BuildScopedVectorProjector,
    BuildScopedVectorRepo,
    collection_for,
    vector_client,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO_CONFIG = REPO_ROOT / "projects" / "demo" / "config.yaml"

#: One project schema serving three UNRELATED domains — the generality gate: the
#: same code handles all three, none named in a code path.
_CONFIG: dict[str, Any] = {
    "chunking": {"max_chars": 1200, "overlap": 100},
    "metadata_schema": {
        "attributes": {
            "case_number": {"type": "string"},
            "sku": {"type": "string"},
            "doi": {"type": "string"},
        }
    },
    "metadata_exposure": {
        "fields": [
            "context.title",
            "context.document_type",
            "context.attributes.case_number",
            "context.attributes.sku",
            "context.attributes.doi",
        ]
    },
}

#: Three unrelated documents: distinct content (so an exact-text query is
#: cosine-nearest its own chunk under the deterministic embedder), distinct
#: document_type + attribute, and a governance field that must NEVER surface.
_DOCS: list[dict[str, Any]] = [
    {
        "filename": "ruling.txt",
        "content": "the appellate ruling on statute alpha is affirmed",
        "context": {
            "title": "Ruling 42",
            "document_type": "ruling",
            "attributes": {"case_number": "42"},
        },
        "governance": {"visibility": "restricted"},
        "attr_key": "case_number",
        "attr_value": "42",
    },
    {
        "filename": "spec.txt",
        "content": "product specification for widget beta revision two",
        "context": {
            "title": "Widget Spec",
            "document_type": "spec",
            "attributes": {"sku": "WGT-2"},
        },
        "governance": {"visibility": "internal"},
        "attr_key": "sku",
        "attr_value": "WGT-2",
    },
    {
        "filename": "note.txt",
        "content": "research note on catalyst gamma and its yield",
        "context": {
            "title": "Catalyst Note",
            "document_type": "note",
            "attributes": {"doi": "10.1/x"},
        },
        "governance": {"visibility": "public"},
        "attr_key": "doi",
        "attr_value": "10.1/x",
    },
]


class _FakeEmbedder:
    """Deterministic 8-dim vectors from sha256(text): the SAME text (stored chunk
    vs query) yields the SAME vector, so an exact-text query is nearest its own
    chunk. No OpenAI key."""

    async def aget_text_embedding(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:8]]


def _embedder() -> BaseEmbedding:
    return cast(BaseEmbedding, _FakeEmbedder())


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def qdrant(migrated: None) -> AsyncIterator[AsyncQdrantClient]:
    client = vector_client()
    yield client
    await client.close()


def _golden_file(tmp_path: Path) -> Path:
    """A minimal one-case golden set — the eval only needs to RUN and score so
    ``builds.eval`` is written and the first-activation gate is satisfied."""
    path = tmp_path / "golden.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "question": "the appellate ruling on statute alpha is affirmed",
                        "mode": "semantic",
                        "expects": {"must_contain_entities": ["nonexistent"]},
                        "min_score": 0.0,
                    }
                ],
            }
        ),
        "utf-8",
    )
    return path


async def _upload(engine: AsyncEngine, project: str) -> None:
    """Drive the REAL upload endpoint against the live engine — the managed
    source and its stashed metadata envelopes are the true capture output."""
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        files = [
            ("files", (d["filename"], d["content"].encode("utf-8"), "text/plain")) for d in _DOCS
        ]
        metadata = {
            d["filename"]: {"context": d["context"], "governance": d["governance"]} for d in _DOCS
        }
        resp = await client.post(
            f"/projects/{project}/uploads",
            files=files,
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["source_id"] is not None
    assert all(f["status"] == "accepted" for f in resp.json()["data"]["files"])


async def _upload_one(project: str, filename: str, content: bytes) -> str:
    """A single-file upload; returns the managed source_id it registered/updated."""
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        resp = await client.post(
            f"/projects/{project}/uploads",
            files=[("files", (filename, content, "text/plain"))],
        )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["source_id"])


async def _cleanup(client: AsyncQdrantClient, project: str) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    engine = _engine()
    async with engine.connect() as conn, conn.begin():
        # chunks are build-scoped (no project column) — they cascade from
        # documents via the composite FK; delete documents (→ chunks), then builds
        await conn.execute(documents.delete().where(documents.c.project == project))
        await conn.execute(builds.delete().where(builds.c.project == project))
        await conn.execute(sources.delete().where(sources.c.project == project))
    await engine.dispose()


async def test_upload_build_eval_activate_metadata_flows_end_to_end(
    qdrant: AsyncQdrantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAPHRAG_UPLOAD_CORPUS_DIR", str(tmp_path))
    engine = _engine()
    project = f"uxc1b-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project, config=_CONFIG)

        # 1) UPLOAD — three unrelated docs with per-file context + governance
        await _upload(engine, project)

        async with engine.connect() as conn:
            # 2) BUILD (document-bearing stages, driven directly — no LLM):
            #    ingest the managed source → documents.metadata carries each
            #    file's captured envelope; clean → chunks; index → vectors.
            build_id: uuid.UUID = (
                await conn.execute(
                    builds.insert()
                    .values(project=project, status="building")
                    .returning(builds.c.id)
                )
            ).scalar_one()
            writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
            managed = [
                s for s in (await list_sources(conn, project, limit=10))[0] if s.kind == "text"
            ]
            assert len(managed) == 1
            report = await ingest_documents(writer, resolve_source(managed[0]))
            assert len(report.documents) == 3
            await conn.commit()

            # CAPTURE → PERSIST: every document row carries its full DR-010
            # envelope (server-stamped system + the client's context/governance)
            doc_rows = await writer.fetch_all(documents)
            by_original = {
                row.metadata["system"]["original_filename"]: row.metadata for row in doc_rows
            }
            for spec in _DOCS:
                envelope = by_original[spec["filename"]]
                assert envelope["schema_version"] == "1.0"
                assert envelope["system"]["connector"] == "upload"
                assert envelope["context"]["title"] == spec["context"]["title"]
                assert envelope["governance"] == spec["governance"]

            for doc in report.documents:
                await clean_document(writer, doc.document_id, doc.raw)
            await conn.commit()
            await _index_build(conn, qdrant, writer)
            await conn.commit()
            await conn.execute(
                builds.update().where(builds.c.id == build_id).values(status="ready")
            )
            await conn.commit()

            # 3) EVAL — the same core path the CLI walks; writes builds.eval.
            # 4) ACTIVATE — a scored first build activates (gate vacuous).
            golden = load_golden(_golden_file(tmp_path))
            policy = load_query_policy(_DEMO_CONFIG)
            driver = graph_driver()
            try:
                async with driver.session() as session:
                    await run_eval(
                        conn,
                        qdrant,
                        session,
                        _embedder(),
                        cast(LLM, None),
                        project,
                        build_id,
                        golden,
                        policy,
                    )
                    check = await activate(conn, qdrant, session, project, build_id)
            finally:
                await driver.close()
            assert check.ok, check.failures

            # 5) QUERY + ENRICH — each doc's chunk carries ONLY its exposed
            #    metadata; governance never leaks; no cross-document bleed
            exposure = load_metadata_exposure(_CONFIG)
            repo = await BuildScopedRepo.for_active_build(conn, project)
            vectors = await BuildScopedVectorRepo.for_active_build(conn, qdrant, project)
            for spec in _DOCS:
                resp = await semantic_search(repo, vectors, _embedder(), spec["content"], top_k=5)
                enriched = await enrich_response_metadata(resp, repo, exposure)
                top = enriched.results[0]
                assert top.text == spec["content"], f"nearest chunk mismatch for {spec['filename']}"
                doc_meta = top.source_refs[0].metadata["document"]
                assert doc_meta["context"]["title"] == spec["context"]["title"]
                assert doc_meta["context"]["document_type"] == spec["context"]["document_type"]
                assert doc_meta["context"]["attributes"] == {spec["attr_key"]: spec["attr_value"]}
                # governance lives in documents.metadata but is NOT allowlisted →
                # it must never reach the agent-visible source_ref (rule 7)
                assert "governance" not in doc_meta
                assert "system" not in doc_meta
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)


async def test_repeated_uploads_merge_into_one_canonical_source(
    qdrant: AsyncQdrantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DR-010: uploads register/UPDATE ONE canonical managed source — a second
    upload to the same project MERGES into it (same source_id, both files
    stashed), never mints a second source-per-upload."""
    monkeypatch.setenv("GRAPHRAG_UPLOAD_CORPUS_DIR", str(tmp_path))
    engine = _engine()
    project = f"uxc1b-merge-{uuid.uuid4().hex[:8]}"
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project, config=_CONFIG)
        first = await _upload_one(project, "a.txt", b"first document")
        second = await _upload_one(project, "b.txt", b"second document")
        assert first == second  # the same managed source updated, not a new one

        async with engine.connect() as conn:
            managed = [
                s for s in (await list_sources(conn, project, limit=10))[0] if s.kind == "text"
            ]
        assert len(managed) == 1  # ONE canonical source for the project's corpus
        # both uploads' files are stashed on it, keyed by their stored names
        files = managed[0].metadata["files"]
        assert len(files) == 2
        originals = {env["system"]["original_filename"] for env in files.values()}
        assert originals == {"a.txt", "b.txt"}
    finally:
        await engine.dispose()
        await _cleanup(qdrant, project)


async def _index_build(
    conn: AsyncConnection, client: AsyncQdrantClient, writer: BuildScopedWriter
) -> None:
    async with graph_driver() as driver, driver.session() as session:
        vectors = await BuildScopedVectorProjector.for_building_build(
            conn, client, writer.project, writer.build_id
        )
        graph = await BuildScopedGraphProjector.for_building_build(
            conn, session, writer.project, writer.build_id
        )
        await index_build(writer, _embedder(), vectors, graph)
