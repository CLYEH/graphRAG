"""Entity mention refs → RESOLVABLE §16 SourceRefs (MCP7, contract v1.1).

An entity's citations are its ``entity_mentions`` rows. The stored text-kind
ref is ``chunk:{content_hash}:{ordinal}`` — rebuild-stable, but NOT a key any
tool accepts: ``chunks.id`` is a UUID and no column stores that string, so
before MCP7 an agent holding the ref could do nothing with it (measured:
``GET /chunks/chunk:3626…:0`` → 422; the MCP surface had the same dead end).
The frozen contract ALLOWED the dead ref — entity results only required
``source_type ∈ {chunk,row}`` while chunk results required uri+offsets (an
asymmetry v1.1 closes).

This module is the single resolution seam all three emitting surfaces share
(semantic entity hits, graph neighbor entities, the ``get_entity`` tool):
text mentions are resolved through the build-scoped two-segment join
(``documents.content_hash`` × ``chunks.ordinal``, DR-006 on both sides) into
refs shaped like the relation chunk-evidence refs already in production —
chunk UUID id + ``source_uri`` + ``quote`` (the mention's surface form) +
chunk offsets — so ``get_chunk`` accepts the id directly. Structured
mentions split into the row shape (``table`` + ``pk``), mirroring row
evidence. A mention that cannot satisfy its shape (unparseable ref, drifted
chunk row, NULL surface_form, unsplittable row ref) is DROPPED and counted —
§22 over-drop, never a schema-invalid emission — and an entity left with
zero resolvable mentions drops entirely (the pre-existing uncitable rule,
now enforced at the tightened v1.1 minimum).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from core.graph.structured import split_row_source_ref
from core.query.results import SourceRef
from core.stores.repo import BuildScopedRepo

#: ``chunk:{content_hash}:{ordinal}`` — the writer's fixed-width-hex hash
#: guarantees the ``:`` separators cannot collide (core/graph/documents.py).
_CHUNK_MENTION_RE = re.compile(r"^chunk:([0-9a-f]{8,}):(\d+)$")


def parse_chunk_mention_ref(ref: str) -> tuple[str, int] | None:
    """``chunk:{content_hash}:{ordinal}`` → ``(content_hash, ordinal)``, or
    None for anything else — stored refs are SoR data but a corrupt one must
    read as uncitable, never crash the query (§22)."""
    match = _CHUNK_MENTION_RE.match(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    return match.group(1), int(match.group(2))


#: Per-entity resolved-ref ceiling for §16 RESULTS (the MCP3 refs-cap
#: precedent): a heavily-mentioned entity resolves to dozens of rich refs
#: (measured: 70 on the dev corpus — each now carrying uri+quote+offsets),
#: and §27.2 needs ≥1, not the roster. ``get_entity`` passes ``cap=None``:
#: introspection IS the full-membership surface, so the cap's escape hatch
#: is a real path, never a dead pointer (#124).
MENTION_REFS_CAP = 8


async def resolved_mention_refs(
    repo: BuildScopedRepo,
    entity_ids: list[uuid.UUID],
    cap: int | None = MENTION_REFS_CAP,
) -> tuple[dict[uuid.UUID, tuple[SourceRef, ...]], int, set[uuid.UUID]]:
    """``(refs_by_entity, dropped_mentions, capped_entities)``.

    One batched read for the mentions plus one for the chunk resolution —
    the whole page costs two queries regardless of entity count. Refs are
    ordered deterministically (by ref id — DB mention order is not
    rerun-stable, the #34 rule) and capped at ``cap`` per entity;
    ``capped_entities`` holds the IDS of entities that lost refs to the cap
    (ids, not a count — the MCP3 provenance lesson: an emitter must be able
    to check whether a capped entity is actually on ITS page before warning,
    or a clipped-off-page cap would mint a false claim)."""
    mentions = await repo.mentions_by_entity(entity_ids)
    pairs = {
        parsed
        for rows in mentions.values()
        for kind, ref, _ in rows
        if kind == "text" and (parsed := parse_chunk_mention_ref(ref)) is not None
    }
    chunks = await repo.chunks_by_content_ref(pairs)

    refs_by_entity: dict[uuid.UUID, tuple[SourceRef, ...]] = {}
    dropped = 0
    capped_entities: set[uuid.UUID] = set()
    for entity_id, rows in mentions.items():
        refs: list[SourceRef] = []
        for kind, ref, surface_form in rows:
            resolved = _resolve_one(kind, ref, surface_form, chunks)
            if resolved is None:
                dropped += 1
            else:
                refs.append(resolved)
        if not refs:
            continue
        refs.sort(key=lambda r: r.id)
        if cap is not None and len(refs) > cap:
            capped_entities.add(entity_id)
            refs = refs[:cap]
        refs_by_entity[entity_id] = tuple(refs)
    return refs_by_entity, dropped, capped_entities


def _resolve_one(
    kind: str,
    ref: str,
    surface_form: str | None,
    chunks: dict[tuple[str, int], Any],
) -> SourceRef | None:
    """One mention row → its v1.1 ref, or None (uncitable — dropped, §22)."""
    if kind == "text":
        parsed = parse_chunk_mention_ref(ref)
        chunk = chunks.get(parsed) if parsed is not None else None
        if (
            chunk is None
            or not isinstance(surface_form, str)
            or not surface_form
            or not isinstance(chunk.source_uri, str)
            or not chunk.source_uri
        ):
            return None  # drifted chunk / NULL quote or uri — cannot satisfy v1.1
        return SourceRef(
            source_type="chunk",
            id=str(chunk.id),
            source_uri=chunk.source_uri,
            metadata={
                "quote": surface_form[:512],
                "start_offset": chunk.start_offset,
                "end_offset": chunk.end_offset,
            },
        )
    if kind == "structured":
        parts = split_row_source_ref(ref) if isinstance(ref, str) else None
        if parts is None or not parts[0] or not parts[1]:
            return None
        return SourceRef(source_type="row", id=ref, metadata={"table": parts[0], "pk": parts[1]})
    return None  # out-of-vocabulary source_kind — uncitable
