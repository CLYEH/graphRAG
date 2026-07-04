"""Clean step: deterministic chunking with exact offsets (DESIGN §5 step 2, C2).

The offsets are load-bearing far beyond retrieval convenience: §27.4 evidence
spans and §27.2 chunk source refs (``source_uri + offsets``) both point back
into the ORIGINAL document text, so every chunk must satisfy
``raw[start_offset:end_offset] == chunk.text`` exactly — an off-by-one here
becomes a mis-quoted citation two pipeline steps later. The invariants
(exact offsets, sequential ordinals, gapless coverage, bounded size, forward
progress) are property-tested with hypothesis (H4 pattern), not just
example-tested.

Chunk size/overlap are 🔧 tunables (chunking 策略, §23); the defaults here
are the code-level fallback until project config (BA1) carries them.
``token_count`` is the cheap ``len//4`` heuristic — good enough for §19
stats; the real tokenizer arrives with the embedding step (C5) and can
backfill.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.stores import tables
from core.stores.repo import BuildScopedWriter

#: 🔧 chunking 策略 defaults (§23) — overridden per project once BA1 lands.
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP = 200


@dataclass(frozen=True)
class Chunk:
    """One §4 chunk row minus the storage-assigned fields."""

    ordinal: int
    text: str
    start_offset: int
    end_offset: int
    token_count: int


def chunk_text(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS, overlap: int = DEFAULT_OVERLAP
) -> list[Chunk]:
    """Split ``text`` into overlapping windows with EXACT offsets.

    Window ends prefer a whitespace boundary (searching back from the hard
    limit) so words survive intact — but never so far back that the next
    window's start (``end - overlap``) would stop advancing: forward progress
    is an invariant, not a hope. Unbreakable runs (no whitespace in range)
    split hard at ``max_chars``. ``overlap`` must be smaller than
    ``max_chars`` for the same reason — validated, not assumed.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive (got {max_chars})")
    if not 0 <= overlap < max_chars:
        raise ValueError(f"overlap must satisfy 0 <= overlap < max_chars (got {overlap})")
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            # prefer ending on whitespace, but only while the next start
            # (end - overlap) still moves strictly forward
            floor = start + overlap + 1
            for position in range(hard_end, floor, -1):
                if text[position - 1].isspace():
                    end = position
                    break
        piece = text[start:end]
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                text=piece,
                start_offset=start,
                end_offset=end,
                token_count=max(1, len(piece) // 4),
            )
        )
        if end == len(text):
            break
        start = end - overlap
    return chunks


class InconsistentChunksError(RuntimeError):
    """The document's stored chunks disagree with a re-run's computation.

    A §27.7 retry re-invoking the clean step must CONVERGE: same text + same
    parameters + a deterministic chunker means identical chunks, so stored
    rows that differ (changed 🔧 params mid-build, or a partial set from a
    commit boundary the orchestrator got wrong) are a real inconsistency —
    silently keeping either version would leave evidence offsets pointing at
    text the retrieval layer doesn't serve. Rebuild the build; don't paper
    over it.
    """

    def __init__(self, document_id: uuid.UUID, stored: int, computed: int) -> None:
        super().__init__(
            f"document {document_id} has {stored} stored chunks but this run "
            f"computed {computed} — parameters changed mid-build or a partial "
            "write survived; rebuild instead of mixing chunkings"
        )
        self.document_id = document_id
        self.stored = stored
        self.computed = computed


async def clean_document(
    writer: BuildScopedWriter,
    document_id: uuid.UUID,
    raw: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Chunk one ingested document and persist the rows (build-scoped, DR-006).

    Idempotent by convergence (§5 冪等 / §27.7 retry): the chunker is
    deterministic, so a re-run recomputes the same chunks — if rows already
    exist and match (ordinal, offsets), they are kept and returned; if they
    disagree, :class:`InconsistentChunksError` refuses to mix two chunkings
    of one document. Returns the chunks so the caller (C3 extraction, tests)
    can proceed without re-reading. ``vector_point_id`` stays NULL — C5 fills
    it when the embedding lands; a chunk row exists before its vector does
    (§5 step order).
    """
    chunks = chunk_text(raw, max_chars=max_chars, overlap=overlap)
    stored = await writer.fetch_all(tables.chunks, tables.chunks.c.document_id == document_id)
    if stored:
        stored_shape = sorted((row.ordinal, row.start_offset, row.end_offset) for row in stored)
        computed_shape = [(c.ordinal, c.start_offset, c.end_offset) for c in chunks]
        if stored_shape != computed_shape:
            raise InconsistentChunksError(document_id, len(stored), len(chunks))
        return chunks
    for chunk in chunks:
        await writer.insert(
            tables.chunks,
            document_id=document_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            token_count=chunk.token_count,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
        )
    return chunks
