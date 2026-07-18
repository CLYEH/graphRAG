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
    """How many rows the clone copied into the child build."""

    documents: int


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
