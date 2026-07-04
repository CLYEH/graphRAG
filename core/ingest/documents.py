"""Persist document payloads into the building build (DESIGN §5 step 1, C2).

All writes go through the build-scoped writer (DR-006) — ingest never sees a
raw connection, so documents land in the validated ``building`` build with
project/build_id injected structurally.

Idempotency (§5/§18): the unit of identity is ``content_hash``. A payload
whose hash already exists IN THIS BUILD is recorded as a ``skipped``
:class:`~core.observability.spec.ItemOutcome` instead of a duplicate row —
so "retry failed only" (§27.7) can re-run ingest wholesale and only the
missing documents are written. The outcomes list is exactly what the
observability layer (C11) will persist as ``pipeline_step_items``; C2 returns
it rather than half-wiring a runs table it doesn't own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from core.ingest.connectors import DocumentPayload, content_hash
from core.observability.spec import ItemOutcome
from core.stores import tables
from core.stores.repo import BuildScopedWriter

#: documents.status value for freshly ingested rows (open vocabulary — the
#: frozen lifecycle enums cover entities/relations, not documents).
INGESTED_STATUS = "ingested"


@dataclass(frozen=True)
class IngestedDocument:
    """What the next step (clean/chunking) needs about one document row."""

    document_id: uuid.UUID
    content_hash: str
    raw: str


@dataclass(frozen=True)
class IngestReport:
    """The step result: the clean handoff plus the §18 item outcomes.

    ``documents`` holds EVERY unique document this batch names — freshly
    written AND already present. A retry that crashed between the document
    commit and its chunks re-runs ingest, gets ``skipped`` outcomes, and
    still receives those documents here; dropping them would strand the
    build with unchunked documents, since this tuple is the only handoff to
    the (idempotent-by-convergence) clean step.
    """

    documents: tuple[IngestedDocument, ...]
    outcomes: tuple[ItemOutcome, ...]


async def ingest_documents(
    writer: BuildScopedWriter, payloads: Iterable[DocumentPayload]
) -> IngestReport:
    """Write payloads as document rows; dedup by content_hash within the build.

    Duplicates — whether already persisted by an earlier (partial) run or
    repeated within this batch — become ``skipped`` outcomes with the §18
    stable ref, never second rows: re-running ingest after a mid-run failure
    must converge on the same document set (§5 冪等).

    The existing-hash set is loaded via full rows (O(corpus bytes) memory) —
    the repo's read surface is deliberately minimal and column projection is
    additive C4+ work; revisit when a real corpus makes this the bottleneck.
    """
    stored = {
        row.content_hash: IngestedDocument(row.id, row.content_hash, row.raw or "")
        for row in await writer.fetch_all(tables.documents)
    }
    handoff: dict[str, IngestedDocument] = {}
    outcomes: list[ItemOutcome] = []
    for payload in payloads:
        digest = content_hash(payload.raw)
        if digest in handoff:
            # in-batch duplicate: its own skipped outcome, one handoff entry
            outcomes.append(ItemOutcome("document", digest, "skipped"))
            continue
        if digest in stored:
            # already committed (earlier run, or partial run that crashed
            # before chunking) — skipped, but STILL handed to clean so the
            # retry converges instead of stranding an unchunked document
            handoff[digest] = stored[digest]
            outcomes.append(ItemOutcome("document", digest, "skipped"))
            continue
        document_id = uuid.uuid4()
        await writer.insert(
            tables.documents,
            id=document_id,
            source_uri=payload.source_uri,
            raw=payload.raw,
            content_hash=digest,
            mime=payload.mime,
            metadata=payload.metadata,
            status=INGESTED_STATUS,
            ingested_at=datetime.now(tz=UTC),
        )
        handoff[digest] = IngestedDocument(document_id, digest, payload.raw)
        outcomes.append(ItemOutcome("document", digest, "ingested"))
    return IngestReport(tuple(handoff.values()), tuple(outcomes))
