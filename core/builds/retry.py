"""RB1-retry-core: seed a child build by cloning the parent's documents.

A retry (``POST /builds/{id}/retry``, DR-013) opens a NEW build recording
``parent_build_id`` and reuses the parent's successful artifacts. The
build_id-scoped artifact tables key rows by a standalone ``id`` PK (DR-006), so
"reuse under a new build_id" is a COPY with a fresh id, never an
``UPDATE build_id``.

**Why only documents (not chunks / the graph layer).** The child re-runs the §5
pipeline (convergent-idempotency resume):

* ``ingest`` is SKIPPED for a retry (``core.builds.stages._is_retry_build``): the
  cloned documents ARE the frozen corpus, and re-reading the project's current
  live sources would break retryBuild's "reuse the parent's artifacts" scope — a
  changed/removed source would fail the re-fetch or drift the content, and a
  source added after the parent build would add documents the parent never had.
* ``clean`` re-chunks each cloned document FRESH under the child build_id. This
  is why chunks are NOT cloned: ``clean_document`` REFUSES to mix two chunkings
  of one document (``InconsistentChunksError``), so a cloned chunk set plus a
  config whose chunk params changed since the parent build would FAIL the retry.
  Re-chunking is deterministic and cheap (no LLM); cloning it buys nothing and
  risks that failure.
* ``graph``/``resolve``/``index``/``summarize`` derive downstream fresh. Cloning
  the graph layer and re-running graph would let a fresh extraction ADD rows the
  dedup index doesn't already hold (dedup blocks exact duplicates, not
  drift/growth). Reusing the graph layer needs the per-item compute-skip (don't
  re-extract successful docs) — the deferred RB1-retry-skip slice.

So RB1-retry-core reuses the ingest output + records lineage; the LLM-cost
saving (rerun ONLY the failed items) is RB1-retry-skip.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables


@dataclass(frozen=True)
class CloneCounts:
    """How many rows a clone copied into the child build. ``documents`` is set by
    :func:`clone_raw_artifacts`; the graph-layer counts by
    :func:`clone_graph_artifacts` (RB1-retry-skip). Each populates its own fields."""

    documents: int = 0
    entities: int = 0
    entity_mentions: int = 0
    relations: int = 0
    relation_evidence: int = 0


async def clone_raw_artifacts(
    conn: AsyncConnection,
    project: str,
    parent_build_id: uuid.UUID,
    child_build_id: uuid.UUID,
) -> CloneCounts:
    """Copy the parent build's ``documents`` into ``child_build_id`` (fresh ids).

    Does NOT commit — the caller (the retry endpoint) owns the transaction, so
    the child build row, this clone, and the job insert commit or roll back as
    one. Returns the copied count (0 = the parent failed at/before ingest, which
    the endpoint refuses). The child's ``clean`` stage re-chunks these documents.

    The copy is set-based (``INSERT ... SELECT gen_random_uuid(), …``): Postgres
    duplicates the rows server-side with fresh ids, so ``POST /retry`` stays
    memory-bounded no matter how large the corpus — the full ``raw`` payloads are
    never streamed into API memory (Codex #100 P2).
    """
    src = tables.documents
    copied = sa.select(
        sa.func.gen_random_uuid(),
        sa.literal(project, type_=src.c.project.type),
        sa.literal(child_build_id, type_=src.c.build_id.type),
        src.c.source_uri,
        src.c.raw,
        src.c.content_hash,
        src.c.mime,
        src.c.metadata,
        src.c.status,
        src.c.ingested_at,
    ).where(src.c.build_id == parent_build_id)
    result = await conn.execute(
        src.insert().from_select(
            [
                "id",
                "project",
                "build_id",
                "source_uri",
                "raw",
                "content_hash",
                "mime",
                "metadata",
                "status",
                "ingested_at",
            ],
            copied,
        )
    )
    return CloneCounts(documents=result.rowcount)


async def clone_graph_artifacts(
    conn: AsyncConnection,
    project: str,
    parent_build_id: uuid.UUID,
    child_build_id: uuid.UUID,
    failed_content_hashes: frozenset[str],
) -> CloneCounts:
    """Copy the parent's SUCCESSFUL text graph-layer artifacts into the child.

    RB1-retry-skip: the child re-extracts only the documents that FAILED graph
    extraction in the parent (``failed_content_hashes``); every other text
    document's entities/relations/mentions/evidence are reused via this clone so
    the LLM is never re-called for them (省成本), and — critically — a failed
    document's PARTIAL committed rows are NOT cloned, so its full fresh
    re-extraction can't collide with drifted ghosts of them.

    The clone is SELECTIVE and TEXT-only (structured artifacts are rebuilt cheaply
    and deterministically by ``extract_structured`` on the child, no LLM), driven
    off the ``chunk:{content_hash}:{ordinal}`` text ``source_ref``/``evidence_ref``
    (``split_part(..., ':', 2)`` = the content_hash): an artifact is reused iff it
    is attributable to a document NOT in the failed set.

    The id-remap between the two build_id-scoped copies is by the frozen per-build
    identities — ``entity_key`` (entities) and ``relation_signature`` (relations)
    — so no old→new id map is materialized in API memory: the four statements are
    pure server-side ``INSERT..SELECT`` (the retry-core #100 P2 memory-bound
    principle). Order matters (FK + the id-remap join): entities → mentions →
    relations → evidence.

    Every statement is IDEMPOTENT for resume (a ``NOT EXISTS`` guard on the
    child's ``entities_by_key`` / ``relations_by_signature`` /
    ``relation_evidence_dedup`` identity), so a re-dispatched retry re-runs the
    clone without violating a unique index. ``embedding_point_id`` is NOT cloned:
    the Qdrant/Neo4j points are per-build and un-cloned, so a copied point-id would
    falsely claim "already embedded"; the child re-embeds fresh. Does NOT commit —
    the caller (the graph stage) owns the transaction.

    The attribution invariant that makes the selective clone sound (a chunk
    evidence's document mentions BOTH relation endpoints — ``extract_documents``
    resets ``accepted_keys`` per chunk) guarantees a cloned relation's endpoints
    were cloned too, so the endpoint joins never drop a wanted relation and the FK
    always holds. Anything the invariant would violate is dropped by the INNER
    joins, never FK-faulted.
    """
    e = tables.entities
    m = tables.entity_mentions
    r = tables.relations
    re_ = tables.relation_evidence
    failed = sorted(failed_content_hashes)  # empty ⇒ notin_([]) is true ⇒ clone all

    def _from_successful_doc(ref_col: sa.ColumnElement[str]) -> sa.ColumnElement[bool]:
        # the text ref is chunk:{content_hash}:{ordinal}; a failed doc's rows stay
        # behind (they'll be re-extracted fresh), everything else is reused.
        return sa.func.split_part(ref_col, ":", 2).notin_(failed)

    # 1) entities: any parent entity with a text mention from a successful doc —
    #    minted fresh id under the child, embedding_point_id left NULL (re-embed).
    child_e = e.alias("child_e")
    has_success_mention = (
        sa.select(sa.literal(1))
        .select_from(m)
        .where(
            m.c.entity_id == e.c.id,
            m.c.source_kind == "text",
            _from_successful_doc(m.c.source_ref),
        )
        .exists()
    )
    entity_exists = (
        sa.select(sa.literal(1))
        .select_from(child_e)
        .where(child_e.c.build_id == child_build_id, child_e.c.entity_key == e.c.entity_key)
        .exists()
    )
    ent_cols = [
        "id",
        "project",
        "build_id",
        "type",
        "canonical_name",
        "entity_key",
        "disambiguator",
        "attributes",
        "status",
        "review_status",
        "created_by",
        "created_at",
        "updated_at",
    ]
    ent_sel = sa.select(
        sa.func.gen_random_uuid(),
        sa.literal(project, type_=e.c.project.type),
        sa.literal(child_build_id, type_=e.c.build_id.type),
        e.c.type,
        e.c.canonical_name,
        e.c.entity_key,
        e.c.disambiguator,
        e.c.attributes,
        e.c.status,
        e.c.review_status,
        e.c.created_by,
        e.c.created_at,
        e.c.updated_at,
    ).where(e.c.build_id == parent_build_id, has_success_mention, ~entity_exists)
    entities_n = (await conn.execute(e.insert().from_select(ent_cols, ent_sel))).rowcount

    # 2) entity_mentions (text only): scoped through entity_id (no build_id column);
    #    remap parent entity → child entity by entity_key.
    src_e = e.alias("src_e")
    dst_e = e.alias("dst_e")
    child_m = m.alias("child_m")
    mention_exists = (
        sa.select(sa.literal(1))
        .select_from(child_m)
        .where(child_m.c.entity_id == dst_e.c.id, child_m.c.source_ref == m.c.source_ref)
        .exists()
    )
    men_sel = (
        sa.select(
            sa.func.gen_random_uuid(),
            dst_e.c.id,
            m.c.source_kind,
            m.c.source_ref,
            m.c.surface_form,
            m.c.confidence,
        )
        .select_from(
            m.join(
                src_e, sa.and_(src_e.c.id == m.c.entity_id, src_e.c.build_id == parent_build_id)
            ).join(
                dst_e,
                sa.and_(
                    dst_e.c.build_id == child_build_id, dst_e.c.entity_key == src_e.c.entity_key
                ),
            )
        )
        .where(m.c.source_kind == "text", _from_successful_doc(m.c.source_ref), ~mention_exists)
    )
    mentions_n = (
        await conn.execute(
            m.insert().from_select(
                ["id", "entity_id", "source_kind", "source_ref", "surface_form", "confidence"],
                men_sel,
            )
        )
    ).rowcount

    # 3) relations: signature-bearing parent relations whose endpoints remap to
    #    child entities (by entity_key) and that have ≥1 chunk evidence from a
    #    successful doc. Endpoint INNER joins keep the FK safe.
    psrc = e.alias("psrc")
    pdst = e.alias("pdst")
    csrc = e.alias("csrc")
    cdst = e.alias("cdst")
    child_r = r.alias("child_r")
    has_success_evidence = (
        sa.select(sa.literal(1))
        .select_from(re_)
        .where(
            re_.c.relation_id == r.c.id,
            re_.c.evidence_type == "chunk",
            _from_successful_doc(re_.c.evidence_ref),
        )
        .exists()
    )
    relation_exists = (
        sa.select(sa.literal(1))
        .select_from(child_r)
        .where(
            child_r.c.build_id == child_build_id,
            child_r.c.relation_signature == r.c.relation_signature,
        )
        .exists()
    )
    rel_cols = [
        "id",
        "project",
        "build_id",
        "src_entity_id",
        "dst_entity_id",
        "type",
        "attributes",
        "relation_signature",
        "status",
        "review_status",
        "created_by",
        "confidence",
        "created_at",
        "updated_at",
    ]
    rel_sel = (
        sa.select(
            sa.func.gen_random_uuid(),
            sa.literal(project, type_=r.c.project.type),
            sa.literal(child_build_id, type_=r.c.build_id.type),
            csrc.c.id,
            cdst.c.id,
            r.c.type,
            r.c.attributes,
            r.c.relation_signature,
            r.c.status,
            r.c.review_status,
            r.c.created_by,
            r.c.confidence,
            r.c.created_at,
            r.c.updated_at,
        )
        .select_from(
            r.join(psrc, psrc.c.id == r.c.src_entity_id)
            .join(pdst, pdst.c.id == r.c.dst_entity_id)
            .join(
                csrc,
                sa.and_(csrc.c.build_id == child_build_id, csrc.c.entity_key == psrc.c.entity_key),
            )
            .join(
                cdst,
                sa.and_(cdst.c.build_id == child_build_id, cdst.c.entity_key == pdst.c.entity_key),
            )
        )
        .where(
            r.c.build_id == parent_build_id,
            r.c.relation_signature.isnot(None),
            has_success_evidence,
            ~relation_exists,
        )
    )
    relations_n = (await conn.execute(r.insert().from_select(rel_cols, rel_sel))).rowcount

    # 4) relation_evidence (chunk only): remap parent relation → child relation by
    #    relation_signature. chunk_id is copied verbatim (dangling by design — not
    #    an FK; offsets are document-absolute so the citation survives re-chunking).
    pr = r.alias("pr")
    cr = r.alias("cr")
    child_ev = re_.alias("child_ev")
    evidence_exists = (
        sa.select(sa.literal(1))
        .select_from(child_ev)
        .where(
            child_ev.c.build_id == child_build_id,
            child_ev.c.evidence_hash == re_.c.evidence_hash,
        )
        .exists()
    )
    ev_sel = (
        sa.select(
            sa.func.gen_random_uuid(),
            cr.c.id,
            sa.literal(child_build_id, type_=re_.c.build_id.type),
            re_.c.evidence_type,
            re_.c.evidence_ref,
            re_.c.chunk_id,
            re_.c.start_offset,
            re_.c.end_offset,
            re_.c.quote,
            re_.c.source_uri,
            re_.c.evidence_hash,
            re_.c.confidence,
        )
        .select_from(
            re_.join(
                pr, sa.and_(pr.c.id == re_.c.relation_id, pr.c.build_id == parent_build_id)
            ).join(
                cr,
                sa.and_(
                    cr.c.build_id == child_build_id,
                    cr.c.relation_signature == pr.c.relation_signature,
                ),
            )
        )
        .where(
            re_.c.evidence_type == "chunk",
            _from_successful_doc(re_.c.evidence_ref),
            ~evidence_exists,
        )
    )
    ev_cols = [
        "id",
        "relation_id",
        "build_id",
        "evidence_type",
        "evidence_ref",
        "chunk_id",
        "start_offset",
        "end_offset",
        "quote",
        "source_uri",
        "evidence_hash",
        "confidence",
    ]
    evidence_n = (await conn.execute(re_.insert().from_select(ev_cols, ev_sel))).rowcount

    return CloneCounts(
        entities=entities_n,
        entity_mentions=mentions_n,
        relations=relations_n,
        relation_evidence=evidence_n,
    )
