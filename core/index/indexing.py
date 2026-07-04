"""Index step: embeddings → Qdrant, entities/relations → Neo4j (DESIGN §5 step 5, C5).

The pipeline's fifth step turns the Postgres source of truth into the two
derived projections queries actually run against (§8): chunk + entity
embeddings land in Qdrant for semantic kNN, and the resolved graph lands in
Neo4j for traversal. The build-scoped projectors (C1c/C1d) already own *how*
each store is written safely (scope-injected payloads, per-write building
revalidation); this step owns *what* gets projected and *when a projection is
skipped*.

Design decisions:

- **The three stores share one identity.** §7 makes ``canonical_id`` the
  cross-store join key; within a build the entity's Postgres row ``id`` is
  that key (relations already reference entities by id), so it is used
  verbatim as the Neo4j node ``canonical_id`` and each entity point's
  ``canonical_id``. Point ids are the same row ids (§4: chunks/entities mint
  fresh uuids per build), which is exactly why re-indexing overwrites in place
  instead of duplicating.
- **Only ``active`` projects.** §17/§7 exclude ``rejected``/``merged``/
  ``needs_review`` rows from projection; resolution (step 4, C4) has already
  stamped those statuses, so this step filters ``entities``/``relations`` to
  ``status='active'``. A relation whose endpoint did not survive resolution
  (one side rejected, so not projected) is **skipped, not failed** — projecting
  it is impossible (no node to attach to) and it is a DESIGN-legitimate state,
  not an error.
- **Idempotent + resumable (§5).** Embedding is the only expensive, failure-
  prone work (an external API), so it is skipped per item whose point id is
  already set (``chunks.vector_point_id`` / ``entities.embedding_point_id``).
  That column IS the retry key: a chunk that failed to embed keeps a NULL
  point id and is naturally re-selected on any re-run, so §27.7
  retry-failed-only needs no separate bookkeeping. Graph MERGE is cheap and
  deterministic, so it is simply re-run (idempotent) rather than skip-tracked.
- **Forward projection only.** The projectors expose no delete (that is C9's
  prune / §19's drift reconciliation), so this step never removes a stale
  point/node — an entity de-projected by a later re-resolution is a drift
  concern, out of scope here by construction.

Failure containment mirrors C3b (§22): a chunk-embedding failure marks its
*document* failed (stable ref = content_hash) and the build continues; an
entity-embedding failure marks that *entity* failed (stable ref = entity_key)
and later entities still run. Graph projection has no external dependency and
no partial-failure surface — a store outage fails the whole step (retried
later), it is not swallowed per item.
"""

from __future__ import annotations

from dataclasses import dataclass

from llama_index.core.embeddings import BaseEmbedding

from core.observability.spec import ItemOutcome
from core.stores import tables
from core.stores.graph import BuildScopedGraphProjector
from core.stores.repo import BuildScopedWriter
from core.stores.vectors import BuildScopedVectorProjector

#: §4 Qdrant point types this step produces (chunk text + entity name). The
#: projector rejects anything outside its own {chunk, entity} vocabulary, so
#: these are named once here for the two upsert sites.
_CHUNK_POINT = "chunk"
_ENTITY_POINT = "entity"


@dataclass(frozen=True)
class IndexReport:
    """What one index pass did (counts are THIS run's actions only).

    ``*_embedded`` count points newly upserted to Qdrant this pass (already-
    embedded items are skipped, so a converged re-run reports zero);
    ``*_projected`` count Neo4j MERGEs (idempotent, so a re-run re-reports
    them); ``relations_skipped`` count active relations held out because an
    endpoint did not survive resolution. ``outcomes`` are the §18 item rows
    the retry boundary (§27.7) reads — documents/entities whose embedding this
    pass completed or failed.
    """

    chunks_embedded: int
    entities_embedded: int
    entities_projected: int
    relations_projected: int
    relations_skipped: int
    outcomes: tuple[ItemOutcome, ...]


async def index_build(
    writer: BuildScopedWriter,
    embedder: BaseEmbedding,
    vectors: BuildScopedVectorProjector,
    graph: BuildScopedGraphProjector,
) -> IndexReport:
    """Project the writer's build into Qdrant + Neo4j (§5 step 5).

    ``writer``, ``vectors`` and ``graph`` must be bound to the SAME building
    ``(project, build_id)`` — the caller (pipeline orchestration) mints all
    three off one Postgres connection so their per-write building guards agree.
    ``embedder`` is provider-blind (§3): only its :meth:`aget_text_embedding`
    is used, one call per item so a single item's failure is contained.
    """
    counts = {
        "chunks_embedded": 0,
        "entities_embedded": 0,
        "entities_projected": 0,
        "relations_projected": 0,
        "relations_skipped": 0,
    }
    outcomes: list[ItemOutcome] = []

    # The per-project collection's vector schema is frozen on first creation,
    # so the size must be the embedder's ACTUAL output dimension — derived from
    # the first vector produced, never hardcoded (§3: the model is 🔧). Ensured
    # exactly once; both point kinds share the collection (§4: one per project).
    ensured = False

    async def _ensure(dim: int) -> None:
        nonlocal ensured
        if not ensured:
            await vectors.ensure_collection(dim)
            ensured = True

    # --- embeddings → Qdrant (chunks, per document for §22 roll-up) ----------
    for doc in await writer.fetch_all(tables.documents):
        pending = sorted(
            (
                chunk
                for chunk in await writer.fetch_all(
                    tables.chunks, tables.chunks.c.document_id == doc.id
                )
                if chunk.vector_point_id is None
            ),
            key=lambda row: row.ordinal,
        )
        if not pending:
            continue  # every chunk already embedded (or the doc has none): no work item
        failed = False
        for chunk in pending:
            try:
                vector = await embedder.aget_text_embedding(chunk.text)
            except Exception:  # noqa: BLE001 — any embed failure = failed item (§22), continue build
                failed = True
                break
            await _ensure(len(vector))
            await vectors.upsert_point(
                chunk.id,
                vector,
                canonical_id=str(chunk.id),
                point_type=_CHUNK_POINT,
                text=chunk.text,
                source_id=chunk.id,
            )
            # mark done AFTER the point lands: a crash between the two re-embeds
            # this chunk next run (point id still NULL) — idempotent overwrite,
            # never a duplicate (point id == chunk id)
            await writer.update(tables.chunks, chunk.id, vector_point_id=chunk.id)
            counts["chunks_embedded"] += 1
        outcomes.append(
            ItemOutcome("document", doc.content_hash, "failed" if failed else "indexed")
        )

    # --- embeddings → Qdrant (entities) + collect the active set for the graph
    active_entities = list(
        await writer.fetch_all(tables.entities, tables.entities.c.status == "active")
    )
    for entity in active_entities:
        if entity.embedding_point_id is not None:
            continue  # already embedded — skip the API call, still projected below
        try:
            vector = await embedder.aget_text_embedding(entity.canonical_name)
        except Exception:  # noqa: BLE001 — one entity's embed failure is contained (§22)
            outcomes.append(ItemOutcome("entity", entity.entity_key, "failed"))
            continue
        await _ensure(len(vector))
        await vectors.upsert_point(
            entity.id,
            vector,
            canonical_id=str(entity.id),
            point_type=_ENTITY_POINT,
            text=entity.canonical_name,
            source_id=entity.id,
        )
        await writer.update(tables.entities, entity.id, embedding_point_id=entity.id)
        counts["entities_embedded"] += 1
        outcomes.append(ItemOutcome("entity", entity.entity_key, "indexed"))

    # --- project entities → Neo4j (nodes before edges) -----------------------
    active_ids = {entity.id for entity in active_entities}
    for entity in active_entities:
        await graph.project_entity(
            str(entity.id), entity.type, entity.status, name=entity.canonical_name
        )
        counts["entities_projected"] += 1

    # --- project relations → Neo4j (only edges with both endpoints projected) -
    for relation in await writer.fetch_all(tables.relations, tables.relations.c.status == "active"):
        if relation.src_entity_id not in active_ids or relation.dst_entity_id not in active_ids:
            # an endpoint did not survive resolution → no node to attach to.
            # DESIGN-legitimate (§17), so skipped and counted, never failed.
            counts["relations_skipped"] += 1
            continue
        await graph.project_relation(
            str(relation.src_entity_id), str(relation.dst_entity_id), relation.type
        )
        counts["relations_projected"] += 1

    return IndexReport(**counts, outcomes=tuple(outcomes))
