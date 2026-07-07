"""Why: C3b is only real if LLM-accepted extractions land as build-scoped rows
that satisfy the frozen §27.4 evidence constraints ON LIVE POSTGRES (quote
non-empty ≤512, offsets present and ordered, source_uri present — the CHECKs
reject anything less), and if the HYBRID promise holds end-to-end: structured
(C3a) and document (C3b) extraction running into the same build share entity
identity through the frozen fingerprints — one "Acme", mentions from both a
row and a chunk. The LLM is a fake (canned JSON); the contract under test is
storage, not the model.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from llama_index.core.llms import LLM, ChatMessage, ChatResponse
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.clean.chunking import clean_document
from core.config import get_settings
from core.graph.documents import extract_documents
from core.graph.ontology import EntityRule, StructuredMapping, TextOntology
from core.graph.structured import extract_structured
from core.ingest.connectors import DocumentPayload
from core.ingest.documents import ingest_documents
from core.resolve import fingerprints
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, entities, entity_mentions, relation_evidence, relations
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_TEXT = "Alice works at Acme. She joined in 2019 and leads the platform team."

_ANSWER = json.dumps(
    {
        "entities": [
            {"type": "Person", "name": "Alice", "confidence": 0.9},
            {"type": "Company", "name": "Acme", "confidence": 0.85},
        ],
        "relations": [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Company",
                "dst_name": "Acme",
                "quote": "Alice works at Acme",
                "confidence": 0.8,
            }
        ],
    }
)

_ONTOLOGY = TextOntology(entity_types=("Person", "Company"), relation_types=("WORKS_AT",))


class _FakeLLM:
    async def achat(self, messages: list[ChatMessage], **_: Any) -> ChatResponse:
        return ChatResponse(message=ChatMessage(role="assistant", content=_ANSWER))


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _building_writer(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    await ensure_project(conn, project)
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def test_hybrid_extraction_shares_identity_on_live_postgres(migrated: None) -> None:
    """§6 end-to-end: a CSV row minting Company 'Acme' (rule) and a text chunk
    naming the same company (LLM) land in ONE build as ONE entity with
    mentions of BOTH source kinds; the §27.4 chunk evidence passes the DB
    CHECKs and its offsets slice the original document text exactly; a re-run
    of both halves writes nothing new."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _building_writer(conn, project)

            report = await ingest_documents(
                writer,
                [
                    DocumentPayload(
                        "mem://people.csv#id=1",
                        json.dumps({"id": "1", "name": "Acme"}, sort_keys=True),
                        "application/json",
                        {"table": "companies", "pk": "1"},
                    ),
                    DocumentPayload("mem://note.txt", _TEXT, "text/plain"),
                ],
            )
            for ingested in report.documents:
                await clean_document(writer, ingested.document_id, ingested.raw)

            mappings = {
                "companies": StructuredMapping(
                    table="companies",
                    entities={"company": EntityRule("Company", "name")},
                )
            }
            structured_report = await extract_structured(writer, mappings)
            llm = cast(LLM, _FakeLLM())
            text_report = await extract_documents(writer, llm, _ONTOLOGY)

            assert structured_report.entities == 1  # Acme via rule
            assert text_report.entities == 1  # only Alice is new — Acme reused

            entity_rows = await writer.fetch_all(entities)
            acme = next(r for r in entity_rows if r.canonical_name == "Acme")
            assert acme.entity_key == fingerprints.entity_key("Company", "Acme")
            assert acme.created_by == "rule"  # first minter wins; llm reused it

            acme_mentions = (
                await conn.execute(
                    entity_mentions.select().where(entity_mentions.c.entity_id == acme.id)
                )
            ).fetchall()
            assert {m.source_kind for m in acme_mentions} == {"structured", "text"}

            relation_rows = await writer.fetch_all(relations)
            assert len(relation_rows) == 1 and relation_rows[0].created_by == "llm"
            ev_rows = await writer.fetch_all(relation_evidence)
            (ev,) = [e for e in ev_rows if e.evidence_type == "chunk"]
            assert _TEXT[ev.start_offset : ev.end_offset] == "Alice works at Acme"
            assert ev.quote == "Alice works at Acme" and ev.source_uri == "mem://note.txt"

            second_structured = await extract_structured(writer, mappings)
            second_text = await extract_documents(writer, llm, _ONTOLOGY)
            assert second_structured.entities == 0 and second_text.entities == 0
            assert second_text.relations == 0 and second_text.evidence == 0
            assert len(await writer.fetch_all(entities)) == len(entity_rows)
            await trans.rollback()
    finally:
        await engine.dispose()
