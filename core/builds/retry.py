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
    one. Returns the copied count. The child's ``clean`` stage re-chunks these
    documents; ``ingest`` dedups them by ``content_hash`` on re-run.
    """
    parent_docs = (
        await conn.execute(
            sa.select(
                tables.documents.c.source_uri,
                tables.documents.c.raw,
                tables.documents.c.content_hash,
                tables.documents.c.mime,
                tables.documents.c.metadata,
                tables.documents.c.status,
                tables.documents.c.ingested_at,
            ).where(tables.documents.c.build_id == parent_build_id)
        )
    ).all()

    doc_rows: list[dict[str, object]] = [
        {
            "id": uuid.uuid4(),
            "project": project,
            "build_id": child_build_id,
            "source_uri": doc.source_uri,
            "raw": doc.raw,
            "content_hash": doc.content_hash,
            "mime": doc.mime,
            "metadata": doc.metadata,
            "status": doc.status,
            "ingested_at": doc.ingested_at,
        }
        for doc in parent_docs
    ]
    if doc_rows:
        await conn.execute(tables.documents.insert(), doc_rows)

    return CloneCounts(documents=len(doc_rows))
