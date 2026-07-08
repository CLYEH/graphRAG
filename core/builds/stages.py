"""Real §5 stage adapters (BA2c-2b) — the production :class:`Stages` the
orchestrator runs, each stage closed over its real dependencies.

:func:`default_stages` is the orchestrator's injection point (``orchestrator.py``
builds a build from a ``Stages`` of six ``(conn, project, build_id) ->
StageResult`` closures). BA2c-1 shipped the control flow with FAKE stages; this
wires the REAL C2–C7 engine. Each adapter:

* is handed a live ``conn`` already inside the orchestrator's per-stage
  transaction, and builds its own writer/projectors off THAT ``conn`` — so the
  ``for_building_build`` status guards (Postgres ``INSERT..WHERE building``,
  Qdrant/Neo4j ``FOR SHARE`` on the shared conn) all agree;
* re-reads its inputs from Postgres (the SoR is the only hand-off between stages
  — convergent idempotency), so a resumed build re-runs each stage and each skips
  its already-done work;
* maps its stage report into a :class:`StageResult`: ``outcomes`` are the §18
  per-item rows the §22 abort and §27.7 retry read; ``detail`` is the stage's own
  count report (folded into ``builds.metrics`` for Health).

The LLM/embedder/store clients are process-wide and closed over here (they are
NOT in Postgres); the per-build Qdrant/Neo4j PROJECTORS are built inside the
index adapter off the handed-in ``conn``. Tests inject fakes for the LLM/embedder
(no OpenAI key needed) and real stores.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime

from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection

from core.builds.config import BuildConfig
from core.builds.orchestrator import StageFn, StageResult, Stages
from core.builds.sources import resolve_source
from core.clean.chunking import clean_document
from core.graph.documents import extract_documents
from core.graph.proposals import persist_proposals
from core.graph.structured import extract_structured
from core.index.indexing import index_build
from core.ingest.connectors import DocumentPayload
from core.ingest.documents import ingest_documents
from core.observability.spec import ItemOutcome
from core.registry.store import Source, list_sources
from core.resolve.resolution import resolve_build
from core.stores.graph import BuildScopedGraphProjector
from core.stores.repo import BuildScopedWriter
from core.stores.tables import STRUCTURED_MIME, documents
from core.stores.vectors import BuildScopedVectorProjector
from core.summarize.communities import summarize_build

#: Page size for reading a project's sources in the ingest stage. Sources are
#: few (a handful per project); one page usually suffices, the loop covers more.
_SOURCE_PAGE = 200


class OntologyRequiredError(ValueError):
    """The build has text-mime documents but its config declares no ontology —
    a config gap the graph stage refuses rather than silently extracting nothing
    from the text. Add an ``ontology`` block to ``projects.config``, or register
    only structured sources."""

    def __init__(self, project: str, build_id: uuid.UUID, text_documents: int) -> None:
        super().__init__(
            f"build {build_id} in project {project} has {text_documents} text "
            "document(s) but config declares no ontology — text extraction would "
            "silently do nothing; add an ontology to projects.config"
        )
        self.project = project
        self.build_id = build_id
        self.text_documents = text_documents


async def _load_sources(conn: AsyncConnection, project: str) -> list[Source]:
    """Every registered source for the project (pages the keyset list)."""
    out: list[Source] = []
    after: tuple[datetime, uuid.UUID] | None = None
    while True:
        page, after = await list_sources(conn, project, limit=_SOURCE_PAGE, after=after)
        out.extend(page)
        if after is None:
            return out


def _all_payloads(sources: list[Source]) -> Iterator[DocumentPayload]:
    """Lazily chain every source's connector stream — the connectors yield
    lazily because sources can be large, so this preserves that rather than
    materializing every payload. A source that cannot be resolved (bad
    kind/uri/metadata) raises when reached; the stage's transaction rolls back
    its partial inserts, so a misconfigured source fails the build loud, never a
    silent partial ingest."""
    for source in sources:
        yield from resolve_source(source)


def _ingest_stage() -> StageFn:
    async def ingest(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        sources = await _load_sources(conn, project)
        report = await ingest_documents(writer, _all_payloads(sources))
        return StageResult(
            outcomes=report.outcomes,
            detail={"sources": len(sources), "documents": len(report.documents)},
        )

    return ingest


def _clean_stage(config: BuildConfig) -> StageFn:
    async def clean(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        docs = await writer.fetch_all(documents)
        chunk_total = 0
        outcomes: list[ItemOutcome] = []
        for doc in docs:
            # clean is deterministic: it either chunks convergently or raises a
            # STRUCTURAL error (param drift) that must fail the build — there is
            # no per-document content failure to isolate, so no per-doc catch.
            produced = await clean_document(
                writer,
                doc.id,
                doc.raw,
                max_chars=config.chunk_max_chars,
                overlap=config.chunk_overlap,
            )
            chunk_total += len(produced)
            # every processed doc is "cleaned": clean_document returns the chunks
            # on both the fresh-insert and already-stored paths, so its return
            # can't distinguish "did work" from "skipped" — and re-deriving that
            # here would duplicate its own convergence check (single-source rule).
            outcomes.append(
                ItemOutcome(item_kind="document", item_ref=doc.content_hash, status="cleaned")
            )
        return StageResult(
            outcomes=tuple(outcomes),
            detail={"documents": len(docs), "chunks": chunk_total},
        )

    return clean


def _graph_stage(config: BuildConfig, chat_model: LLM) -> StageFn:
    async def graph(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        # C3a: deterministic structured rule-mapping extraction.
        structured = await extract_structured(writer, config.structured_mappings)
        outcomes = list(structured.outcomes)
        detail: dict[str, object] = {
            "structured": {
                "entities": structured.entities,
                "relations": structured.relations,
                "mentions": structured.mentions,
                "evidence": structured.evidence,
            }
        }
        if config.ontology is not None:
            # C3b: LLM document extraction, then C3c: persist type proposals.
            text = await extract_documents(writer, chat_model, config.ontology)
            outcomes.extend(text.outcomes)
            await persist_proposals(
                conn, project, text.proposals, policy=config.ontology_proposal_policy
            )
            detail["text"] = {
                "entities": text.entities,
                "relations": text.relations,
                "mentions": text.mentions,
                "evidence": text.evidence,
                "proposals": len(text.proposals),
                "discarded": len(text.discarded),
            }
        else:
            # An ontology-less build with text documents is a config gap, not a
            # silent skip (config.py's contract: enforced at run time here).
            text_docs = await writer.fetch_all(documents, documents.c.mime != STRUCTURED_MIME)
            if text_docs:
                raise OntologyRequiredError(project, build_id, len(text_docs))
        return StageResult(outcomes=tuple(outcomes), detail=detail)

    return graph


def _resolve_stage(config: BuildConfig) -> StageFn:
    async def resolve(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        report = await resolve_build(conn, writer, config.resolution)
        # resolve has no natural per-item retry unit (§18) — aggregate counts only.
        return StageResult(outcomes=(), detail=asdict(report))

    return resolve


def _index_stage(
    embedder: BaseEmbedding, vector_client: AsyncQdrantClient, graph_session: AsyncSession
) -> StageFn:
    async def index(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        # projectors are per-build and MUST be built off the handed-in conn so
        # their FOR SHARE building-guards share the transaction with the writer.
        vectors = await BuildScopedVectorProjector.for_building_build(
            conn, vector_client, project, build_id
        )
        graph = await BuildScopedGraphProjector.for_building_build(
            conn, graph_session, project, build_id
        )
        report = await index_build(writer, embedder, vectors, graph)
        return StageResult(
            outcomes=report.outcomes,
            detail={
                "chunks_embedded": report.chunks_embedded,
                "entities_embedded": report.entities_embedded,
                "entities_projected": report.entities_projected,
                "relations_projected": report.relations_projected,
                "relations_skipped": report.relations_skipped,
            },
        )

    return index


def _summarize_stage(chat_model: LLM) -> StageFn:
    async def summarize(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> StageResult:
        writer = await BuildScopedWriter.for_building_build(conn, project, build_id)
        report = await summarize_build(writer, chat_model)
        return StageResult(
            outcomes=report.outcomes,
            detail={"communities": report.communities, "written": report.written},
        )

    return summarize


def default_stages(
    config: BuildConfig,
    *,
    chat_model: LLM,
    embedder: BaseEmbedding,
    vector_client: AsyncQdrantClient,
    graph_session: AsyncSession,
) -> Stages:
    """The six real §5 stage adapters wired over ``config`` and the process-wide
    LLM/embedder/store clients. The caller (BA2e's build trigger) constructs the
    deps once — ``chat_model``/``embedder`` via :mod:`core.llm.factory`, the
    Qdrant client and a Neo4j session from :mod:`core.stores` — and hands the
    result to ``run_build``."""
    return Stages(
        ingest=_ingest_stage(),
        clean=_clean_stage(config),
        graph=_graph_stage(config, chat_model),
        resolve=_resolve_stage(config),
        index=_index_stage(embedder, vector_client, graph_session),
        summarize=_summarize_stage(chat_model),
    )
