"""Why: the index step (§5 step 5) is the seam between the Postgres source of
truth and the two projections queries actually run against (§8). Its DECISIONS
— which rows project, which are skipped, that re-running embeds nothing new,
and that one item's embed failure is contained (§22) — are logic that must
hold independent of the live stores. The store adapters' own guarantees
(scope-injected payloads, per-write building revalidation, kNN filtering) are
proven against real Qdrant/Neo4j in test_index_indexing_integration.py; these
in-memory fakes recheck the orchestration fast, which is also where coverage
of indexing.py comes from (integration tests are excluded from the gate).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from llama_index.core.embeddings import BaseEmbedding

from core.index.indexing import index_build
from core.stores import tables
from core.stores.graph import BuildScopedGraphProjector
from core.stores.repo import BuildScopedWriter
from core.stores.vectors import BuildScopedVectorProjector


def _matches(row: dict[str, Any], predicate: Any) -> bool:
    """Evaluate a simple ``col == value`` predicate against a row dict."""
    return bool(row[predicate.left.name] == predicate.right.value)


class _FakeWriter:
    """In-memory stand-in for BuildScopedWriter: reads return snapshots,
    updates mutate the backing dicts (so a second index pass sees point ids)."""

    project = "p1"
    build_id = uuid.uuid4()

    def __init__(self) -> None:
        self.rows: dict[Any, list[dict[str, Any]]] = {
            tables.documents: [],
            tables.chunks: [],
            tables.entities: [],
            tables.relations: [],
        }

    async def fetch_all(self, table: Any, *where: Any) -> list[SimpleNamespace]:
        rows = self.rows[table]
        for predicate in where:
            rows = [r for r in rows if _matches(r, predicate)]
        return [SimpleNamespace(**r) for r in rows]

    async def update(self, table: Any, row_id: Any, /, **values: Any) -> None:
        row = next(r for r in self.rows[table] if r["id"] == row_id)
        row.update(values)


class _FakeVectors:
    """Captures ensure_collection + upsert_point calls (no server)."""

    def __init__(self) -> None:
        self.ensured: list[int] = []
        self.points: list[SimpleNamespace] = []

    async def ensure_collection(self, vector_size: int) -> None:
        self.ensured.append(vector_size)

    async def upsert_point(
        self,
        point_id: uuid.UUID,
        vector: list[float],
        *,
        canonical_id: str,
        point_type: str,
        text: str,
        source_id: uuid.UUID,
    ) -> None:
        self.points.append(
            SimpleNamespace(
                point_id=point_id,
                vector=vector,
                canonical_id=canonical_id,
                point_type=point_type,
                text=text,
                source_id=source_id,
            )
        )


class _FakeGraph:
    """Captures project_entity / project_relation calls (no server)."""

    def __init__(self) -> None:
        self.entities: list[SimpleNamespace] = []
        self.relations: list[SimpleNamespace] = []

    async def project_entity(
        self, canonical_id: str, entity_type: str, status: str, name: str | None = None
    ) -> None:
        self.entities.append(
            SimpleNamespace(canonical_id=canonical_id, type=entity_type, status=status, name=name)
        )

    async def project_relation(self, src: str, dst: str, rel_type: str) -> None:
        self.relations.append(SimpleNamespace(src=src, dst=dst, type=rel_type))


class _FakeEmbedder:
    """Deterministic vectors; raises for any text in ``fail_on`` so §22
    containment is exercised. Records every call to prove skip-on-re-run."""

    def __init__(self, dim: int = 3, fail_on: frozenset[str] = frozenset()) -> None:
        self.dim = dim
        self.fail_on = fail_on
        self.calls: list[str] = []

    async def aget_text_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        if text in self.fail_on:
            raise RuntimeError(f"embed failed for {text!r}")
        # first component encodes the text length so vectors are distinguishable
        return [float(len(text))] + [0.0] * (self.dim - 1)


async def _run(
    writer: _FakeWriter,
    embedder: _FakeEmbedder,
    vectors: _FakeVectors,
    graph: _FakeGraph,
) -> Any:
    return await index_build(
        cast(BuildScopedWriter, writer),
        cast(BaseEmbedding, embedder),
        cast(BuildScopedVectorProjector, vectors),
        cast(BuildScopedGraphProjector, graph),
    )


def _add_document(
    writer: _FakeWriter, *, content_hash: str, source_uri: str = "s://d"
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    writer.rows[tables.documents].append(
        {"id": doc_id, "content_hash": content_hash, "source_uri": source_uri, "mime": "text/plain"}
    )
    return doc_id


def _add_chunk(
    writer: _FakeWriter, *, document_id: uuid.UUID, ordinal: int, text: str
) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    writer.rows[tables.chunks].append(
        {
            "id": chunk_id,
            "document_id": document_id,
            "ordinal": ordinal,
            "text": text,
            "vector_point_id": None,
        }
    )
    return chunk_id


def _add_entity(
    writer: _FakeWriter,
    *,
    name: str,
    etype: str = "Company",
    status: str = "active",
    key: str | None = None,
) -> uuid.UUID:
    entity_id = uuid.uuid4()
    writer.rows[tables.entities].append(
        {
            "id": entity_id,
            "type": etype,
            "canonical_name": name,
            "status": status,
            "entity_key": key or f"k:{etype}:{name}",
            "embedding_point_id": None,
        }
    )
    return entity_id


def _add_relation(
    writer: _FakeWriter,
    *,
    src: uuid.UUID,
    dst: uuid.UUID,
    rtype: str = "PARTNERS",
    status: str = "active",
) -> uuid.UUID:
    rel_id = uuid.uuid4()
    writer.rows[tables.relations].append(
        {"id": rel_id, "src_entity_id": src, "dst_entity_id": dst, "type": rtype, "status": status}
    )
    return rel_id


async def test_projects_chunks_entities_relations_with_shared_identity() -> None:
    """The happy path pins the cross-store identity (§7): every Qdrant point
    and Neo4j node is keyed by the Postgres row id, the source key follows the
    point type (§4), and the collection is created ONCE at the embedder's
    actual dimension (§3: model is 🔧, size is never hardcoded)."""
    writer = _FakeWriter()
    doc = _add_document(writer, content_hash="h1")
    c0 = _add_chunk(writer, document_id=doc, ordinal=0, text="alpha")
    c1 = _add_chunk(writer, document_id=doc, ordinal=1, text="beta")
    e_acme = _add_entity(writer, name="Acme")
    e_globex = _add_entity(writer, name="Globex")
    _add_relation(writer, src=e_acme, dst=e_globex)

    embedder = _FakeEmbedder(dim=3)
    vectors, graph = _FakeVectors(), _FakeGraph()
    report = await _run(writer, embedder, vectors, graph)

    assert (report.chunks_embedded, report.entities_embedded) == (2, 2)
    assert (report.entities_projected, report.relations_projected) == (2, 1)
    assert report.relations_skipped == 0
    # collection ensured exactly once, at the embedder's real output dimension
    assert vectors.ensured == [3]

    # chunk points: source key = chunk id, canonical_id = str(row id), text = chunk text
    chunk_points = {p.point_id: p for p in vectors.points if p.point_type == "chunk"}
    assert set(chunk_points) == {c0, c1}
    assert chunk_points[c0].canonical_id == str(c0) and chunk_points[c0].source_id == c0
    assert chunk_points[c0].text == "alpha"
    # entity points: canonical_id == source == entity row id
    entity_points = {p.point_id: p for p in vectors.points if p.point_type == "entity"}
    assert set(entity_points) == {e_acme, e_globex}
    assert entity_points[e_acme].canonical_id == str(e_acme) == str(entity_points[e_acme].source_id)
    assert entity_points[e_acme].text == "Acme"

    # graph: nodes keyed by entity id, edge endpoints are those same ids
    assert {n.canonical_id for n in graph.entities} == {str(e_acme), str(e_globex)}
    assert all(n.status == "active" for n in graph.entities)
    (edge,) = graph.relations
    assert (edge.src, edge.dst, edge.type) == (str(e_acme), str(e_globex), "PARTNERS")

    # §18 outcomes: one per document embedded, one per entity embedded
    assert SimpleNamespace(item_kind="document", item_ref="h1", status="indexed") in [
        SimpleNamespace(item_kind=o.item_kind, item_ref=o.item_ref, status=o.status)
        for o in report.outcomes
    ]
    assert sum(o.item_kind == "entity" and o.status == "indexed" for o in report.outcomes) == 2

    # point ids written back to Postgres (the skip/retry key)
    assert all(r["vector_point_id"] == r["id"] for r in writer.rows[tables.chunks])
    assert all(r["embedding_point_id"] == r["id"] for r in writer.rows[tables.entities])


async def test_re_running_embeds_nothing_new_but_reprojects_graph() -> None:
    """§5 idempotency: the second pass skips every already-embedded item (point
    id set) — no embedder calls, no new outcomes — while the graph MERGE is
    cheap and simply re-runs (idempotent)."""
    writer = _FakeWriter()
    doc = _add_document(writer, content_hash="h1")
    _add_chunk(writer, document_id=doc, ordinal=0, text="alpha")
    _add_entity(writer, name="Acme")

    embedder = _FakeEmbedder()
    first = await _run(writer, embedder, _FakeVectors(), _FakeGraph())
    assert first.chunks_embedded == 1 and first.entities_embedded == 1
    calls_after_first = len(embedder.calls)

    vectors2, graph2 = _FakeVectors(), _FakeGraph()
    second = await _run(writer, embedder, vectors2, graph2)
    assert (second.chunks_embedded, second.entities_embedded) == (0, 0)
    assert len(embedder.calls) == calls_after_first  # no new embed API calls
    assert vectors2.points == [] and vectors2.ensured == []  # nothing to upsert
    assert second.outcomes == ()  # no work items this pass
    assert second.entities_projected == 1  # graph re-projected (idempotent)


async def test_only_active_rows_project_and_dangling_relations_are_skipped() -> None:
    """§17/§7: rejected/merged/needs_review rows are excluded from BOTH
    projections; a relation whose endpoint did not survive resolution is
    skipped (DESIGN-legitimate) — never failed, never projected against a
    missing node."""
    writer = _FakeWriter()
    live = _add_entity(writer, name="Acme", status="active")
    rejected = _add_entity(writer, name="Bad", status="rejected")
    merged = _add_entity(writer, name="Gone", status="merged")
    # active edge to a rejected endpoint → skip; active edge live→live → project
    live2 = _add_entity(writer, name="Globex", status="active")
    _add_relation(writer, src=live, dst=rejected)
    good = _add_relation(writer, src=live, dst=live2)
    # an inactive relation never projects regardless of endpoints
    _add_relation(writer, src=live, dst=live2, status="rejected")

    vectors, graph = _FakeVectors(), _FakeGraph()
    report = await _run(writer, _FakeEmbedder(), vectors, graph)

    projected_ids = {n.canonical_id for n in graph.entities}
    assert projected_ids == {str(live), str(live2)}  # rejected/merged excluded
    assert str(rejected) not in projected_ids and str(merged) not in projected_ids
    # only the fully-live active edge projected; dangling skipped, inactive ignored
    assert report.entities_embedded == 2 and report.entities_projected == 2
    assert report.relations_projected == 1 and report.relations_skipped == 1
    (edge,) = graph.relations
    assert edge.src == str(live) and edge.dst == str(live2)
    assert good  # the projected edge is the live→live one


async def test_chunk_embed_failure_marks_document_failed_and_continues() -> None:
    """§22 containment: a chunk that fails to embed marks its DOCUMENT failed
    (stable ref = content_hash) and breaks that document, but a later document
    still indexes — and the failed document's already-embedded chunks kept
    their point ids, so a retry resumes rather than re-embeds them."""
    writer = _FakeWriter()
    d1 = _add_document(writer, content_hash="h1")
    c_ok = _add_chunk(writer, document_id=d1, ordinal=0, text="good")
    _add_chunk(writer, document_id=d1, ordinal=1, text="POISON")  # fails
    _add_chunk(writer, document_id=d1, ordinal=2, text="after")  # never reached (break)
    d2 = _add_document(writer, content_hash="h2")
    _add_chunk(writer, document_id=d2, ordinal=0, text="fine")

    embedder = _FakeEmbedder(fail_on=frozenset({"POISON"}))
    vectors, graph = _FakeVectors(), _FakeGraph()
    report = await _run(writer, embedder, vectors, graph)

    statuses = {(o.item_ref): o.status for o in report.outcomes if o.item_kind == "document"}
    assert statuses == {"h1": "failed", "h2": "indexed"}
    assert report.chunks_embedded == 2  # "good" and "fine"; "after" not reached
    assert "after" not in embedder.calls  # break stops the failed document's remaining chunks
    # the chunk that succeeded before the failure kept its point id (resume, not redo)
    by_id = {r["id"]: r for r in writer.rows[tables.chunks]}
    assert by_id[c_ok]["vector_point_id"] == c_ok


async def test_entity_embed_failure_is_isolated_but_still_projected() -> None:
    """An entity embedding is independent per item: a failure marks THAT
    entity failed (ref = entity_key) and later entities still embed. The
    entity is still a graph node — embedding and graph projection are
    independent projections, so a missing embedding never drops it from the
    traversal graph."""
    writer = _FakeWriter()
    good = _add_entity(writer, name="Acme", key="k-acme")
    bad = _add_entity(writer, name="BOOM", key="k-boom")
    tail = _add_entity(writer, name="Globex", key="k-globex")

    embedder = _FakeEmbedder(fail_on=frozenset({"BOOM"}))
    vectors, graph = _FakeVectors(), _FakeGraph()
    report = await _run(writer, embedder, vectors, graph)

    assert report.entities_embedded == 2  # Acme + Globex
    failed = {
        o.item_ref for o in report.outcomes if o.item_kind == "entity" and o.status == "failed"
    }
    assert failed == {"k-boom"}
    # all three active entities are graph nodes regardless of embedding outcome
    assert {n.canonical_id for n in graph.entities} == {str(good), str(bad), str(tail)}
    # the failed entity kept a NULL embedding point id → naturally retried next run
    assert {r["id"]: r for r in writer.rows[tables.entities]}[bad]["embedding_point_id"] is None


async def test_empty_build_touches_no_store() -> None:
    """A build with nothing to project makes no collection and no writes — the
    dimension is derived from a real vector, so with none produced there is
    nothing to ensure."""
    writer = _FakeWriter()
    vectors, graph = _FakeVectors(), _FakeGraph()
    report = await _run(writer, _FakeEmbedder(), vectors, graph)
    assert report == report.__class__(0, 0, 0, 0, 0, ())
    assert vectors.ensured == [] and vectors.points == []
    assert graph.entities == [] and graph.relations == []
