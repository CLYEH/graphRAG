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
from core.query.results import QueryWarning, SourceRef
from core.stores.repo import BuildScopedRepo

#: ``chunk:{content_hash}:{ordinal}`` — the writer's fixed-width-hex hash
#: guarantees the ``:`` separators cannot collide (core/graph/documents.py).
#: The ordinal is ASCII-ONLY ``[0-9]`` (Codex #127 r3: ``\d`` matches
#: Unicode digits — ``١`` would int() to 1 and cite a chunk the stored ref
#: never identified) and BOUNDED at 9 digits (r2): an unbounded ``\d+``
#: let a corrupt ref raise instead of dropping — ``int()`` fails past
#: CPython's digit limit and anything over 2^31-1 fails at the Postgres
#: INTEGER bind; 9 digits stays inside both, and the writer's real ordinals
#: are tiny.
_CHUNK_MENTION_RE = re.compile(r"^chunk:([0-9a-f]{8,}):([0-9]{1,9})$")


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
) -> tuple[dict[uuid.UUID, tuple[SourceRef, ...]], dict[uuid.UUID, int], set[uuid.UUID]]:
    """``(refs_by_entity, dropped_by_entity, capped_entities)``.

    One batched read for the mentions plus one for the chunk resolution —
    the whole page costs two queries regardless of entity count. Refs are
    ordered deterministically (by ref id — DB mention order is not
    rerun-stable, the #34 rule) and capped at ``cap`` per entity. Both loss
    channels carry PER-ENTITY provenance, never bare counts (the MCP3
    lesson): ``dropped_by_entity`` maps each entity to its unresolvable
    mention count and ``capped_entities`` holds the ids that lost refs to
    the cap — an emitter checks its OWN page before warning, so an off-page
    loss can never mint a false claim about returned results (see
    :func:`mention_warnings`)."""
    mentions = await repo.mentions_by_entity(entity_ids)
    pairs = {
        parsed
        for rows in mentions.values()
        for kind, ref, _ in rows
        if kind == "text" and (parsed := parse_chunk_mention_ref(ref)) is not None
    }
    chunks = await repo.chunks_by_content_ref(pairs)

    refs_by_entity: dict[uuid.UUID, tuple[SourceRef, ...]] = {}
    dropped_by_entity: dict[uuid.UUID, int] = {}
    capped_entities: set[uuid.UUID] = set()
    for entity_id, rows in mentions.items():
        refs: list[SourceRef] = []
        for kind, ref, surface_form in rows:
            resolved = _resolve_one(kind, ref, surface_form, chunks)
            if resolved is None:
                dropped_by_entity[entity_id] = dropped_by_entity.get(entity_id, 0) + 1
            else:
                refs.append(resolved)
        if not refs:
            continue
        refs.sort(key=lambda r: r.id)
        if cap is not None and len(refs) > cap:
            capped_entities.add(entity_id)
            refs = refs[:cap]
        refs_by_entity[entity_id] = tuple(refs)
    return refs_by_entity, dropped_by_entity, capped_entities


#: Message stems for the two mention-loss warnings. The affected entity ids
#: ride IN the message (builder/parser siblings — the MCP3 refs-cap
#: precedent): hybrid fuses mode responses and clips entities, so it must be
#: able to re-derive each warning for ITS page or a clipped entity's loss
#: would mint a false claim about the fused results (Codex #127 r4).
MENTION_DROP_STEM = "unresolvable mention citation(s) omitted on returned entities"
MENTION_CAP_STEM = f"entity mention refs capped at {MENTION_REFS_CAP} per entity"


def mention_warnings(
    returned_entity_ids: set[uuid.UUID],
    dropped_by_entity: dict[uuid.UUID, int],
    capped_entities: set[uuid.UUID],
) -> tuple[QueryWarning, ...]:
    """The two mention-loss warnings, PAGE-EXACT (single source for every
    §16 emitter — semantic and graph must not drift, Codex #127).

    Only losses on RETURNED entities warn: an off-page entity's cap or drop
    never charged this response (the MCP3 provenance rule). A dropped-whole
    entity is the emitter's own hit-level drop accounting, not ours. The
    messages NAME the affected entity ids (with per-entity counts on the
    drop side) so hybrid can rebuild them exactly for its fused page via the
    sibling parsers below."""
    warnings: list[QueryWarning] = []
    dropped_on_page = {
        str(eid): count
        for eid in sorted(returned_entity_ids, key=str)
        if (count := dropped_by_entity.get(eid, 0))
    }
    if dropped_on_page:
        detail = ", ".join(f"{eid}={count}" for eid, count in dropped_on_page.items())
        warnings.append(
            QueryWarning(
                "PARTIAL_RESULTS",
                f"{sum(dropped_on_page.values())} {MENTION_DROP_STEM}: {detail} "
                "(uncitable mention or projection drift — see Health for drift)",
            )
        )
    capped_on_page = sorted((str(eid) for eid in returned_entity_ids & capped_entities), key=str)
    if capped_on_page:
        warnings.append(
            QueryWarning(
                "TRUNCATED",
                f"{MENTION_CAP_STEM} — returned entity(ies) {', '.join(capped_on_page)} "
                "affected; the full mention list is available via get_entity (§22)",
            )
        )
    return tuple(warnings)


def parse_mention_drop_warning(message: str) -> dict[uuid.UUID, int] | None:
    """``{entity_id: dropped_count}`` a drop warning names — None for any
    other message. Accepts hybrid's ``[mode] `` origin prefix; the parser
    lives beside the builder (one owner of the message shape)."""
    text = message.split("] ", 1)[-1] if message.startswith("[") else message
    marker = f" {MENTION_DROP_STEM}: "
    if marker not in text:
        return None
    detail = text.split(marker, 1)[1].split(" (", 1)[0]
    parsed: dict[uuid.UUID, int] = {}
    for pair in detail.split(", "):
        raw_id, sep, raw_count = pair.partition("=")
        if not sep:
            return None
        try:
            parsed[uuid.UUID(raw_id)] = int(raw_count)
        except (ValueError, AttributeError):
            return None
    return parsed or None


def parse_mention_cap_warning(message: str) -> set[uuid.UUID] | None:
    """The capped entity ids a cap warning names — None for any other
    message. Same prefix tolerance and ownership as the drop parser."""
    text = message.split("] ", 1)[-1] if message.startswith("[") else message
    marker = f"{MENTION_CAP_STEM} — returned entity(ies) "
    if not text.startswith(marker):
        return None
    listed = text[len(marker) :].split(" affected", 1)[0]
    try:
        return {uuid.UUID(raw_id) for raw_id in listed.split(", ")}
    except (ValueError, AttributeError):
        return None


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
