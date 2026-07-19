"""Why: BA2c-2b is the first end-to-end wiring of the C1–C11 engine into the
orchestrator — ``default_stages`` closes the six real §5 stages over their deps,
and the orchestrator's control flow (unit-proven with fakes in
test_builds_orchestrator_integration.py) now drives REAL ingest→…→summarize
against live Postgres + Qdrant + Neo4j. Only a live run proves the adapters wire
correctly: each builds its writer/projectors off the handed-in conn, re-reads
from the SoR, and maps its report into the §18 StageResult the orchestrator
records. A deterministic fake LLM/embedder keeps it reproducible with no key.

Two arcs: a structured-only corpus runs all six stages to ``ready`` and lands
rows in every store; and the graph stage's config-gap guard fails a build whose
config declares no ontology while text documents were ingested.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.config import load_build_config
from core.builds.orchestrator import run_build
from core.builds.retry import clone_raw_artifacts
from core.builds.stages import default_stages
from core.config import get_settings
from core.observability.recorder import StepReport, record_run
from core.observability.spec import ItemOutcome
from core.registry import add_source, create_job, create_project, get_job
from core.stores import tables
from core.stores.graph import graph_driver
from core.stores.vectors import collection_for, vector_client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_WIPE_PROJECT = "MATCH (n:Entity {project: $project}) DETACH DELETE n"

_STRUCTURED_CONFIG = {
    "structured_mappings": {
        "people": {
            "entities": {
                "person": {"entity_type": "Person", "name_column": "name"},
                "company": {"entity_type": "Company", "name_column": "company"},
            },
            "relations": [{"relation_type": "WORKS_AT", "src": "person", "dst": "company"}],
        }
    }
}


class _FakeLLM:
    """Deterministic community summary — the only LLM call a structured-only
    build makes (graph skips the LLM when config has no ontology)."""

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        answer = json.dumps({"title": "Cluster", "summary": "They work together.", "rating": 5})
        return SimpleNamespace(message=SimpleNamespace(content=answer))


class _FakeEmbedder:
    """Deterministic 4-dim vectors so projection is real without an OpenAI key."""

    async def aget_text_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0, 0.0]


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture()
async def stores(migrated: None) -> AsyncIterator[tuple[AsyncQdrantClient, AsyncSession]]:
    client = vector_client()
    driver = graph_driver()
    async with driver.session() as session:
        yield client, session
    await client.close()
    await driver.close()


def _stages_for(
    config_raw: dict[str, Any], client: AsyncQdrantClient, session: AsyncSession
) -> Any:
    return default_stages(
        load_build_config(config_raw),
        chat_model=cast(LLM, _FakeLLM()),
        embedder=cast(BaseEmbedding, _FakeEmbedder()),
        vector_client=client,
        graph_session=session,
    )


async def _count(engine: AsyncEngine, table: sa.Table, project: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.project == project)
            )
        ).scalar_one()


async def _cleanup(
    engine: AsyncEngine, client: AsyncQdrantClient, session: AsyncSession, project: str
) -> None:
    if await client.collection_exists(collection_for(project)):
        await client.delete_collection(collection_for(project))
    await (await session.run(_WIPE_PROJECT, {"project": project})).consume()
    async with engine.connect() as conn, conn.begin():
        step_ids = (
            sa.select(tables.pipeline_steps.c.id)
            .join(tables.pipeline_runs, tables.pipeline_steps.c.run_id == tables.pipeline_runs.c.id)
            .where(tables.pipeline_runs.c.project == project)
        )
        await conn.execute(
            tables.pipeline_step_items.delete().where(
                tables.pipeline_step_items.c.step_id.in_(step_ids)
            )
        )
        await conn.execute(
            tables.pipeline_steps.delete().where(
                tables.pipeline_steps.c.run_id.in_(
                    sa.select(tables.pipeline_runs.c.id).where(
                        tables.pipeline_runs.c.project == project
                    )
                )
            )
        )
        for table in (
            tables.pipeline_runs,
            tables.community_reports,
            tables.entities,  # cascades to relations/mentions/evidence/merge_candidates
            tables.documents,  # cascades to chunks
            tables.jobs,
            tables.sources,
            tables.builds,
        ):
            await conn.execute(table.delete().where(table.c.project == project))
        await conn.execute(tables.projects.delete().where(tables.projects.c.name == project))


async def _write_csv(tmp_path: Path) -> Path:
    csv = tmp_path / "people.csv"
    csv.write_text("id,name,company\n1,Alice,Acme\n2,Bob,Acme\n", encoding="utf-8")
    return csv


async def test_structured_build_runs_all_six_stages_to_ready_across_all_stores(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        csv = await _write_csv(tmp_path)
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            await add_source(
                conn,
                project,
                uri=csv.as_uri(),
                kind="structured",
                metadata={"table": "people", "pk_column": "id"},
            )
            job = await create_job(conn, project, "build")

        outcome = await run_build(
            engine, project, job.id, _stages_for(_STRUCTURED_CONFIG, client, session)
        )

        assert outcome.status == "ready"
        assert not outcome.cancelled and outcome.error is None

        # every store carries this build's output: 2 rows → 2 docs, 3 entities
        # (Alice, Bob, Acme) joined by 2 WORKS_AT, and ≥1 community summary.
        assert await _count(engine, tables.documents, project) == 2
        assert await _count(engine, tables.entities, project) == 3
        assert await _count(engine, tables.relations, project) == 2
        assert await _count(engine, tables.community_reports, project) >= 1

        # Qdrant: the collection exists with the build's points; Neo4j: 3 nodes.
        assert await client.collection_exists(collection_for(project))
        record = await (
            await session.run("MATCH (n:Entity {project: $p}) RETURN count(n) AS c", {"p": project})
        ).single()
        assert record is not None and record["c"] == 3

        async with engine.connect() as conn:
            done_job = await get_job(conn, job.id)
        assert done_job is not None and done_job.status == "done" and done_job.progress == 1.0
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_text_source_without_ontology_fails_the_build_at_graph(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """A build that ingested text documents but whose config declares no
    ontology is a config gap the graph stage refuses (OntologyRequiredError) —
    the build fails at graph rather than silently extracting nothing."""
    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        (tmp_path / "note.txt").write_text("Acme partners with Globex.", encoding="utf-8")
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            await add_source(conn, project, uri=tmp_path.as_uri(), kind="text", metadata={})
            job = await create_job(conn, project, "build")

        outcome = await run_build(engine, project, job.id, _stages_for({}, client, session))

        assert outcome.status == "failed"
        assert not outcome.cancelled
        assert outcome.error is not None and "graph:" in outcome.error
        assert "ontology" in outcome.error
        # ingest + clean ran before the gap; the text document is present.
        assert await _count(engine, tables.documents, project) == 1
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_retry_build_ingest_skips_live_sources(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """RB1-retry (Codex #100 P1): a retry child reprocesses the PARENT's corpus
    (cloned in as documents), so its ingest stage must NOT re-read the project's
    current live sources — otherwise a source added/changed after the failed
    parent would drift the child out of the promised "reuse the parent's
    artifacts" scope (or fail the re-fetch). Discriminating setup: a live CSV
    source is registered (a normal build ingests it as 2 documents — see
    test_structured_build), and the retry child is pre-seeded with ONE unrelated
    cloned document. If ingest re-read sources for a retry (the bug), the child
    would gain the CSV's 2 rows; the assertion that ONLY the clone remains fails."""
    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        csv = await _write_csv(tmp_path)
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            # a live source a NON-retry ingest would turn into documents
            await add_source(
                conn,
                project,
                uri=csv.as_uri(),
                kind="structured",
                metadata={"table": "people", "pk_column": "id"},
            )
            parent = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="failed")
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            # a retry CHILD (parent_build_id set) pre-seeded with ONE cloned doc
            child = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building", parent_build_id=parent)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.execute(
                tables.documents.insert().values(
                    project=project,
                    build_id=child,
                    source_uri="file:///cloned.txt",
                    raw="cloned body",
                    content_hash="cloned-hash",
                    mime="text/plain",
                    status="ingested",
                )
            )

        # run ONLY the ingest stage against the retry child
        ingest = _stages_for(_STRUCTURED_CONFIG, client, session).ingest
        async with engine.connect() as conn, conn.begin():
            result = await ingest(conn, project, child)

        # ingest reused the frozen cloned corpus, reading NO live source
        assert result.detail == {"retry_reused_documents": 1}
        assert result.outcomes == ()
        async with engine.connect() as conn:
            hashes = (
                (
                    await conn.execute(
                        sa.select(tables.documents.c.content_hash).where(
                            tables.documents.c.build_id == child
                        )
                    )
                )
                .scalars()
                .all()
            )
        # exactly the clone — the CSV's rows were never ingested into the child
        assert list(hashes) == ["cloned-hash"]
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


_RETRY_ONTOLOGY_CONFIG = {
    "ontology": {
        "entity_types": ["Person"],
        "relation_types": ["KNOWS"],
        "proposal_policy": "review",
    },
    "chunking": {"max_chars": 1200, "overlap": 200},
}

# doc B SHARES the entity "Alice" with doc A (the successful one) — so when the
# retry re-extracts B, its "Alice" must CONVERGE on the entity cloned from A (by
# the frozen entity_key), gaining a second mention, exactly as a full rebuild
# would. A cloned entity re-mentioned across the success/failure boundary is the
# crux the oracle must exercise (Codex gate-2 nit).
_DOC_A = "Alice knows Bob"
_DOC_B = "Alice knows Carol"


def _extraction(a_name: str, b_name: str, text: str) -> str:
    return json.dumps(
        {
            "entities": [
                {"type": "Person", "name": a_name, "confidence": 0.9},
                {"type": "Person", "name": b_name, "confidence": 0.9},
            ],
            "relations": [
                {
                    "src_type": "Person",
                    "src_name": a_name,
                    "type": "KNOWS",
                    "dst_type": "Person",
                    "dst_name": b_name,
                    "quote": text,  # verbatim in the chunk (§27.4)
                    "confidence": 0.8,
                }
            ],
        }
    )


class _ExtractLLM:
    """Deterministic per-chunk text extraction; RAISES on a configured chunk to
    simulate a transient graph failure for one document. Records its calls so a
    test can prove the retry re-extracted ONLY the failed doc."""

    def __init__(self, answers: dict[str, str], fail: set[str] | None = None) -> None:
        self._answers = answers
        self._fail = fail or set()
        self.calls: list[str] = []

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        user = str(messages[-1].content)
        self.calls.append(user)
        if user in self._fail:
            raise RuntimeError("transient LLM failure")
        return SimpleNamespace(message=SimpleNamespace(content=self._answers[user]))


async def _make_building(
    engine: AsyncEngine, project: str, *, parent: uuid.UUID | None = None
) -> uuid.UUID:
    async with engine.connect() as conn, conn.begin():
        return cast(
            "uuid.UUID",
            (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building", parent_build_id=parent)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one(),
        )


async def _seed_build_job(
    engine: AsyncEngine,
    project: str,
    build_id: uuid.UUID,
    config: dict[str, Any],
    *,
    kind: str = "build",
) -> None:
    """Seed the job that BUILT a build, carrying its config_snapshot — so
    `build_config_snapshot(build_id)` returns `config`. RB1-retry-skip's config
    guard reads the parent's and child's snapshots to confirm the child runs the
    parent's config before reusing its graph layer."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            tables.jobs.insert().values(
                project=project,
                kind=kind,
                build_id=build_id,
                status="done",
                config_snapshot=config,
            )
        )


async def _seed_text_doc(
    engine: AsyncEngine, project: str, build_id: uuid.UUID, h: str, raw: str
) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            tables.documents.insert().values(
                project=project,
                build_id=build_id,
                source_uri=f"file:///{h}.txt",
                raw=raw,
                content_hash=h,
                mime="text/plain",
                status="ingested",
            )
        )


async def _run_clean_then_graph(
    engine: AsyncEngine, stages: Any, project: str, build_id: uuid.UUID
) -> Any:
    async with engine.connect() as conn, conn.begin():
        await stages.clean(conn, project, build_id)
    async with engine.connect() as conn, conn.begin():
        return await stages.graph(conn, project, build_id)


async def _graph_fingerprint(
    engine: AsyncEngine, build_id: uuid.UUID
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[tuple[str, str]]]:
    """A build's post-graph identity: entity_keys, relation_signatures, evidence
    hashes, and (entity_key, mention source_ref) pairs — everything the merge of
    (cloned successes + re-extracted failures) must equal in a full rebuild."""
    async with engine.connect() as conn:
        ents = (
            await conn.execute(
                sa.select(tables.entities.c.id, tables.entities.c.entity_key).where(
                    tables.entities.c.build_id == build_id
                )
            )
        ).all()
        key_by_id = {e.id: e.entity_key for e in ents}
        mentions = (
            await conn.execute(
                sa.select(
                    tables.entity_mentions.c.entity_id, tables.entity_mentions.c.source_ref
                ).where(tables.entity_mentions.c.entity_id.in_(list(key_by_id) or [uuid.uuid4()]))
            )
        ).all()
        sigs = (
            (
                await conn.execute(
                    sa.select(tables.relations.c.relation_signature).where(
                        tables.relations.c.build_id == build_id
                    )
                )
            )
            .scalars()
            .all()
        )
        ev = (
            (
                await conn.execute(
                    sa.select(tables.relation_evidence.c.evidence_hash).where(
                        tables.relation_evidence.c.build_id == build_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return (
        frozenset(key_by_id.values()),
        frozenset(s for s in sigs if s is not None),
        frozenset(ev),
        frozenset((key_by_id[m.entity_id], m.source_ref) for m in mentions),
    )


async def test_retry_skip_reextracts_only_the_failed_doc_and_equals_a_full_rebuild(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """RB1-retry-skip end-to-end oracle. A parent build extracts doc A but FAILS
    doc B at graph (transient LLM error). The retry child reuses A's graph layer
    (cloned) and re-extracts ONLY B — and because the failure was transient, B now
    succeeds. The merged child graph must be IDENTICAL to a full clean rebuild's
    (where both docs succeed): same entity_keys, relation_signatures, evidence
    hashes, and mention refs. This is the whole point — reuse must not lose or
    drift anything the full run would have produced, under a deterministic LLM.
    The call log proves the child re-called the LLM for B ONLY (A was reused)."""
    client, session = stores
    engine = _engine()
    project = _proj()
    answers = {
        _DOC_A: _extraction("Alice", "Bob", _DOC_A),
        _DOC_B: _extraction("Alice", "Carol", _DOC_B),  # shares "Alice" with doc A
    }
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)

        # ---- PARENT: A extracted, B fails at graph ----
        parent = await _make_building(engine, project)
        await _seed_build_job(engine, project, parent, _RETRY_ONTOLOGY_CONFIG)
        await _seed_text_doc(engine, project, parent, "hash-a", _DOC_A)
        await _seed_text_doc(engine, project, parent, "hash-b", _DOC_B)
        parent_stages = default_stages(
            load_build_config(_RETRY_ONTOLOGY_CONFIG),
            chat_model=cast(LLM, _ExtractLLM(answers, fail={_DOC_B})),
            embedder=cast(BaseEmbedding, _FakeEmbedder()),
            vector_client=client,
            graph_session=session,
        )
        parent_graph = await _run_clean_then_graph(engine, parent_stages, project, parent)
        # record the graph step's items so the retry can read the failed set
        # (record_run owns its OWN transaction — hand it a no-txn connection),
        # then terminalize the parent 'failed' (as run_build would on a failed item)
        async with engine.connect() as conn:
            await record_run(
                conn,
                project,
                parent,
                "build",
                [StepReport("graph", parent_graph.outcomes)],
                verbosity="failures",
            )
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == parent).values(status="failed")
            )
        assert {(o.item_ref, o.status) for o in parent_graph.outcomes} == {
            ("hash-a", "extracted"),
            ("hash-b", "failed"),
        }

        # ---- RETRY CHILD: clone docs, re-run; B recovers ----
        child = await _make_building(engine, project, parent=parent)
        await _seed_build_job(engine, project, child, _RETRY_ONTOLOGY_CONFIG, kind="retry")
        async with engine.connect() as conn, conn.begin():
            await clone_raw_artifacts(conn, project, parent, child)
        child_llm = _ExtractLLM(answers, fail=set())  # transient failure gone
        child_stages = default_stages(
            load_build_config(_RETRY_ONTOLOGY_CONFIG),
            chat_model=cast(LLM, child_llm),
            embedder=cast(BaseEmbedding, _FakeEmbedder()),
            vector_client=client,
            graph_session=session,
        )
        await _run_clean_then_graph(engine, child_stages, project, child)
        # the compute-skip: the child re-called the LLM for B's chunk ONLY — A's
        # graph layer was reused via the clone, never re-extracted
        assert child_llm.calls == [_DOC_B]

        # ---- FULL REBUILD: fresh build, both docs succeed ----
        fresh = await _make_building(engine, project)
        await _seed_text_doc(engine, project, fresh, "hash-a", _DOC_A)
        await _seed_text_doc(engine, project, fresh, "hash-b", _DOC_B)
        fresh_stages = default_stages(
            load_build_config(_RETRY_ONTOLOGY_CONFIG),
            chat_model=cast(LLM, _ExtractLLM(answers, fail=set())),
            embedder=cast(BaseEmbedding, _FakeEmbedder()),
            vector_client=client,
            graph_session=session,
        )
        await _run_clean_then_graph(engine, fresh_stages, project, fresh)

        # ---- ORACLE: the reused+re-extracted child graph == the full rebuild ----
        child_fp = await _graph_fingerprint(engine, child)
        fresh_fp = await _graph_fingerprint(engine, fresh)
        assert child_fp == fresh_fp
        # non-trivial: 3 entities (Alice/Bob/Carol), 2 KNOWS relations
        assert len(child_fp[0]) == 3 and len(child_fp[1]) == 2
        # the CRUX: the shared entity Alice was CLONED from doc A and then
        # RE-MENTIONED when doc B re-extracted — converging on the cloned row by
        # its frozen entity_key rather than minting a twin. So exactly one entity
        # carries BOTH docs' mention refs, as a full rebuild would (gate-2 nit).
        refs_by_key: dict[str, set[str]] = {}
        for key, ref in child_fp[3]:
            refs_by_key.setdefault(key, set()).add(ref)
        assert [refs for refs in refs_by_key.values() if len(refs) == 2] == [
            {"chunk:hash-a:0", "chunk:hash-b:0"}
        ]
        # P2 (Codex #103 R2): cloned chunk-evidence's chunk_id was REMAPPED to the
        # child's re-chunked chunk (same content_hash+ordinal), so every citation
        # resolves under the active child — never a dangling parent chunk id.
        async with engine.connect() as conn:
            ev_chunk_ids = (
                (
                    await conn.execute(
                        sa.select(tables.relation_evidence.c.chunk_id).where(
                            tables.relation_evidence.c.build_id == child
                        )
                    )
                )
                .scalars()
                .all()
            )
            child_chunk_ids = set(
                (
                    await conn.execute(
                        sa.select(tables.chunks.c.id).where(tables.chunks.c.build_id == child)
                    )
                )
                .scalars()
                .all()
            )
        assert ev_chunk_ids and all(cid in child_chunk_ids for cid in ev_chunk_ids)
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_retry_falls_back_to_full_rederive_when_the_parent_ran_resolve(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """fork C (Codex #103): §22 TOLERATES an under-threshold graph failure, so a
    parent can fail doc B at graph, RUN resolve (merging entities into 'merged'
    audit rows + null-signature relations), then fail later. That post-resolve
    graph layer can't be faithfully reused by the pre-resolve-shaped selective
    clone, so the retry must DETECT resolve ran and fall back to a FULL re-derive —
    re-extracting EVERY doc, never the clone+skip. Discriminating: the LLM is
    called for BOTH docs (the skip path would call it for the failed B only)."""
    client, session = stores
    engine = _engine()
    project = _proj()
    answers = {
        _DOC_A: _extraction("Alice", "Bob", _DOC_A),
        _DOC_B: _extraction("Alice", "Carol", _DOC_B),
    }
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
        parent = await _make_building(engine, project)
        await _seed_build_job(engine, project, parent, _RETRY_ONTOLOGY_CONFIG)
        await _seed_text_doc(engine, project, parent, "hash-a", _DOC_A)
        await _seed_text_doc(engine, project, parent, "hash-b", _DOC_B)
        parent_graph = await _run_clean_then_graph(
            engine,
            default_stages(
                load_build_config(_RETRY_ONTOLOGY_CONFIG),
                chat_model=cast(LLM, _ExtractLLM(answers, fail={_DOC_B})),
                embedder=cast(BaseEmbedding, _FakeEmbedder()),
                vector_client=client,
                graph_session=session,
            ),
            project,
            parent,
        )
        # the parent TOLERATED the graph failure and RAN resolve, then failed later:
        # record BOTH a graph step (B failed) AND a resolve step
        async with engine.connect() as conn:
            await record_run(
                conn,
                project,
                parent,
                "build",
                [StepReport("graph", parent_graph.outcomes), StepReport("resolve", ())],
                verbosity="failures",
            )
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == parent).values(status="failed")
            )

        child = await _make_building(engine, project, parent=parent)
        await _seed_build_job(engine, project, child, _RETRY_ONTOLOGY_CONFIG, kind="retry")
        async with engine.connect() as conn, conn.begin():
            await clone_raw_artifacts(conn, project, parent, child)
        child_llm = _ExtractLLM(answers, fail=set())
        await _run_clean_then_graph(
            engine,
            default_stages(
                load_build_config(_RETRY_ONTOLOGY_CONFIG),
                chat_model=cast(LLM, child_llm),
                embedder=cast(BaseEmbedding, _FakeEmbedder()),
                vector_client=client,
                graph_session=session,
            ),
            project,
            child,
        )
        # FULL re-derive: the resolve-ran guard suppressed the clone+skip, so the
        # LLM re-extracted BOTH docs — not just the failed B the skip path would do
        assert set(child_llm.calls) == {_DOC_A, _DOC_B}
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_retry_falls_back_to_full_rederive_when_the_parent_config_is_unavailable(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """Codex #103 R2 P1: a LEGACY parent whose producing job recorded no config
    (config_snapshot is schema-nullable) makes the retry endpoint's pin fall back to
    the LIVE project config — so the child's clone (parent-config rows) plus its
    re-extraction (live config) would MIX two configs. The graph stage must confirm
    the pin held (parent config present AND equal to the child's) before clone+skip;
    when the parent config is unavailable it falls back to a FULL re-derive.
    Discriminating: NO build job is seeded for the parent, so its config is None →
    the LLM re-extracts BOTH docs, not just the failed one."""
    client, session = stores
    engine = _engine()
    project = _proj()
    answers = {
        _DOC_A: _extraction("Alice", "Bob", _DOC_A),
        _DOC_B: _extraction("Alice", "Carol", _DOC_B),
    }
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
        parent = await _make_building(engine, project)
        # NB: NO _seed_build_job for the parent → build_config_snapshot(parent) is None
        await _seed_text_doc(engine, project, parent, "hash-a", _DOC_A)
        await _seed_text_doc(engine, project, parent, "hash-b", _DOC_B)
        parent_graph = await _run_clean_then_graph(
            engine,
            default_stages(
                load_build_config(_RETRY_ONTOLOGY_CONFIG),
                chat_model=cast(LLM, _ExtractLLM(answers, fail={_DOC_B})),
                embedder=cast(BaseEmbedding, _FakeEmbedder()),
                vector_client=client,
                graph_session=session,
            ),
            project,
            parent,
        )
        async with engine.connect() as conn:
            await record_run(
                conn,
                project,
                parent,
                "build",
                [StepReport("graph", parent_graph.outcomes)],
                verbosity="failures",
            )
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == parent).values(status="failed")
            )

        child = await _make_building(engine, project, parent=parent)
        await _seed_build_job(engine, project, child, _RETRY_ONTOLOGY_CONFIG, kind="retry")
        async with engine.connect() as conn, conn.begin():
            await clone_raw_artifacts(conn, project, parent, child)
        child_llm = _ExtractLLM(answers, fail=set())
        await _run_clean_then_graph(
            engine,
            default_stages(
                load_build_config(_RETRY_ONTOLOGY_CONFIG),
                chat_model=cast(LLM, child_llm),
                embedder=cast(BaseEmbedding, _FakeEmbedder()),
                vector_client=client,
                graph_session=session,
            ),
            project,
            child,
        )
        # the parent config is unavailable → full re-derive, both docs re-extracted
        assert set(child_llm.calls) == {_DOC_A, _DOC_B}
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_retry_full_rederives_when_an_entity_entangles_failed_and_success_docs(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """Codex #103 R3+R4: an entity (or relation) with a first-write from BOTH a
    failed doc and a successful doc may carry the FAILED doc's partial scalars (rows
    are first-write-wins), which the selective clone retains and preload freezes. The
    retry must detect the entanglement and fall back to a FULL re-derive — re-
    extracting EVERY doc — rather than a selective reuse. Discriminating: the LLM is
    called for BOTH docs; the clone+skip path would call it for the failed one only."""
    client, session = stores
    engine = _engine()
    project = _proj()
    answers = {
        _DOC_A: _extraction("Alice", "Bob", _DOC_A),
        _DOC_B: _extraction("Alice", "Carol", _DOC_B),
    }
    try:
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
        parent = await _make_building(engine, project)
        await _seed_build_job(engine, project, parent, _RETRY_ONTOLOGY_CONFIG)
        await _seed_text_doc(engine, project, parent, "hash-a", _DOC_A)
        await _seed_text_doc(engine, project, parent, "hash-b", _DOC_B)
        # seed a parent ENTITY entangled across hash-a (success) + hash-b (failed):
        # a text mention from each doc on the same entity
        async with engine.connect() as conn, conn.begin():
            ent = (
                await conn.execute(
                    tables.entities.insert()
                    .values(
                        project=project,
                        build_id=parent,
                        type="Person",
                        canonical_name="Alice",
                        entity_key="fpv2:alice",
                        status="active",
                        created_by="llm",
                    )
                    .returning(tables.entities.c.id)
                )
            ).scalar_one()
            for h in ("hash-a", "hash-b"):
                await conn.execute(
                    tables.entity_mentions.insert().values(
                        entity_id=ent, source_kind="text", source_ref=f"chunk:{h}:0"
                    )
                )
        async with engine.connect() as conn:
            await record_run(
                conn,
                project,
                parent,
                "build",
                [
                    StepReport(
                        "graph",
                        (
                            ItemOutcome("document", "hash-a", "extracted"),
                            ItemOutcome("document", "hash-b", "failed"),
                        ),
                    )
                ],
                verbosity="failures",
            )
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == parent).values(status="failed")
            )

        child = await _make_building(engine, project, parent=parent)
        await _seed_build_job(engine, project, child, _RETRY_ONTOLOGY_CONFIG, kind="retry")
        async with engine.connect() as conn, conn.begin():
            await clone_raw_artifacts(conn, project, parent, child)
        child_llm = _ExtractLLM(answers, fail=set())
        await _run_clean_then_graph(
            engine,
            default_stages(
                load_build_config(_RETRY_ONTOLOGY_CONFIG),
                chat_model=cast(LLM, child_llm),
                embedder=cast(BaseEmbedding, _FakeEmbedder()),
                vector_client=client,
                graph_session=session,
            ),
            project,
            child,
        )
        # entanglement → full re-derive: BOTH docs re-extracted, not just failed B
        assert set(child_llm.calls) == {_DOC_A, _DOC_B}
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()


async def test_xlsx_source_ingests_per_row_text_documents(
    stores: tuple[AsyncQdrantClient, AsyncSession], tmp_path: Path
) -> None:
    """SRC1 end-to-end wiring proof on live stores: a registered xlsx source
    (column mapping in its metadata) resolves, ingests ONE text document per
    content row with a citable ``#row=`` identity — and, because the rows are
    TEXT documents, the graph stage's ontology gate fires for an ontology-less
    config exactly as it does for a .txt directory. The gate firing IS the
    evidence the rows entered the text pipeline (not the structured lane)."""
    import openpyxl
    from openpyxl.worksheet.worksheet import Worksheet

    client, session = stores
    engine = _engine()
    project = _proj()
    try:
        book = tmp_path / "guide.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        assert isinstance(ws, Worksheet)
        ws.append(["編號", "標題(必填)", "內容詳情(必填)", "位置"])
        ws.append([1.0, "深海探索廳", "介紹深潛器海淵一號。", "B1"])
        ws.append([2, "海洋劇場", "球幕電影與導覽。", None])
        ws.append([3, None, None, None])  # pre-numbered template row: skipped
        wb.save(book)
        async with engine.connect() as conn, conn.begin():
            await create_project(conn, name=project)
            await add_source(
                conn,
                project,
                uri=book.as_uri(),
                kind="xlsx",
                metadata={
                    "title_column": "標題",
                    "body_column": "內容詳情",
                    "id_column": "編號",
                    "extra_columns": ["位置"],
                    "label": "導覽",
                },
            )
            job = await create_job(conn, project, "build")

        outcome = await run_build(engine, project, job.id, _stages_for({}, client, session))

        assert outcome.status == "failed"
        assert outcome.error is not None and "ontology" in outcome.error
        # two content rows ingested (the template row skipped), each a TEXT
        # document carrying its per-row citation fragment
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(tables.documents.c.source_uri, tables.documents.c.mime).where(
                        tables.documents.c.project == project
                    )
                )
            ).all()
        assert sorted(uri.rsplit("#", 1)[1] for uri, _ in rows) == ["row=1", "row=2"]
        assert all(mime == "text/plain" for _, mime in rows)
    finally:
        await _cleanup(engine, client, session, project)
        await engine.dispose()
