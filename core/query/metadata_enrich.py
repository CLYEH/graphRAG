"""Enrich retrieval ``source_ref.metadata`` with document metadata (DR-010 rule 6/7).

A retrieval response cites chunks; the DR-010 document metadata envelope lives on
``documents.metadata`` (stored ONCE, rule 5). This boundary pass resolves each
chunk source_ref back to its document through the Postgres SoR (chunk → document,
build-scoped, no per-chunk duplication and no cross-build drift) and merges the
EXPOSED slice of that document's envelope — the fields the project's
``metadata_exposure`` allowlist names — into the ref's ``metadata`` under a
``document`` key. Fail-closed: with no allowlist, nothing is resolved and the
response is returned unchanged, so a governance field is never leaked merely
because it sits in JSONB (rule 7).

It runs at the QUERY BOUNDARY (the API query router / MCP tool), after any
modality has produced the response, so it is modality-agnostic — semantic, graph,
and hybrid chunk refs all get the same treatment from one place, and the
retrieval modalities stay unaware of metadata exposure. The read goes through the
build-scoped repo (DR-006), so it can never cross build scopes.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any

from core.metadata.schema import MetadataExposure
from core.query.results import McpResponse, RetrievalResult, SourceRef
from core.stores import tables
from core.stores.repo import BuildScopedRepo


async def enrich_response_metadata(
    response: McpResponse, repo: BuildScopedRepo, exposure: MetadataExposure
) -> McpResponse:
    """Return ``response`` with every chunk source_ref's ``metadata`` carrying the
    exposed slice of its document's envelope. Unchanged when the allowlist is
    empty (fail-closed) or no chunk ref resolves to exposed metadata."""
    if not exposure.fields:
        return response
    chunk_refs = _chunk_ref_ids(response)
    if not chunk_refs:
        return response
    exposed_by_ref = await _exposed_metadata_by_ref(repo, chunk_refs, exposure)
    if not exposed_by_ref:
        return response
    return dataclasses.replace(
        response,
        results=tuple(_enrich_result(result, exposed_by_ref) for result in response.results),
    )


#: A stable chunk ref is ``chunk:<content_hash>:<ordinal>`` (see
#: ``core.graph.documents.chunk_source_ref``) — the form entity/graph hits cite
#: (``core.query.semantic._entity_result``). Vector hits instead cite the chunk's
#: ``chunks.id`` UUID. Both are ``source_type == "chunk"`` but resolve to their
#: document differently, so enrichment must recognize BOTH shapes or allowlisted
#: document metadata silently never appears on entity/graph results.
_STABLE_CHUNK_PREFIX = "chunk:"


def _chunk_ref_ids(response: McpResponse) -> set[str]:
    """The RAW id strings of every ``chunk`` source_ref (both UUID and stable
    forms, unparsed) — the enricher resolves each shape to its document below and
    keys the result by this exact string so ``_enrich_ref`` can match it back."""
    return {
        ref.id
        for result in response.results
        for ref in result.source_refs
        if ref.source_type == "chunk"
    }


def _as_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _stable_ref_content_hash(raw: str) -> str | None:
    """The document ``content_hash`` a stable chunk ref names, or None if ``raw``
    is not the ``chunk:<content_hash>:<ordinal>`` form. The ordinal is not needed:
    DR-010 metadata is document-level (rule 5), and the content_hash names the
    document within the build-scoped repo."""
    if not raw.startswith(_STABLE_CHUNK_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) != 3 or not parts[1] or not parts[2].isdigit():
        return None
    return parts[1]


async def _exposed_metadata_by_ref(
    repo: BuildScopedRepo, ref_ids: set[str], exposure: MetadataExposure
) -> dict[str, dict[str, Any]]:
    """Resolve each raw chunk ref → its document envelope, projected through the
    allowlist and keyed by the raw ref string. Two ref shapes resolve two ways:
    a UUID via ``chunks.id`` → ``document_id``; a stable ``chunk:<hash>:<ordinal>``
    via ``documents.content_hash`` (the ordinal is irrelevant to document-level
    metadata). Only refs whose document has a non-empty exposed projection appear."""
    envelope_by_ref: dict[str, Any] = {}
    await _resolve_uuid_refs(repo, ref_ids, envelope_by_ref)
    await _resolve_stable_refs(repo, ref_ids, envelope_by_ref)
    exposed: dict[str, dict[str, Any]] = {}
    for raw, envelope in envelope_by_ref.items():
        if isinstance(envelope, Mapping):
            projected = exposure.project(envelope)
            if projected:
                exposed[raw] = projected
    return exposed


async def _resolve_uuid_refs(
    repo: BuildScopedRepo, ref_ids: set[str], envelope_by_ref: dict[str, Any]
) -> None:
    """UUID chunk refs (vector hits): ``chunks.id`` → ``document_id`` → envelope."""
    uuid_by_ref = {raw: parsed for raw in ref_ids if (parsed := _as_uuid(raw)) is not None}
    if not uuid_by_ref:
        return
    chunk_rows = await repo.fetch_all(
        tables.chunks, tables.chunks.c.id.in_(set(uuid_by_ref.values()))
    )
    document_by_chunk = {row.id: row.document_id for row in chunk_rows}
    document_ids = set(document_by_chunk.values())
    if not document_ids:
        return
    document_rows = await repo.fetch_all(tables.documents, tables.documents.c.id.in_(document_ids))
    envelope_by_document = {row.id: row.metadata for row in document_rows}
    for raw, chunk_id in uuid_by_ref.items():
        document_id = document_by_chunk.get(chunk_id)
        if document_id is not None:
            envelope_by_ref[raw] = envelope_by_document.get(document_id)


async def _resolve_stable_refs(
    repo: BuildScopedRepo, ref_ids: set[str], envelope_by_ref: dict[str, Any]
) -> None:
    """Stable chunk refs (entity/graph hits): ``documents.content_hash`` → envelope."""
    hash_by_ref = {
        raw: content_hash
        for raw in ref_ids
        if (content_hash := _stable_ref_content_hash(raw)) is not None
    }
    if not hash_by_ref:
        return
    document_rows = await repo.fetch_all(
        tables.documents, tables.documents.c.content_hash.in_(set(hash_by_ref.values()))
    )
    envelope_by_hash = {row.content_hash: row.metadata for row in document_rows}
    for raw, content_hash in hash_by_ref.items():
        if content_hash in envelope_by_hash:
            envelope_by_ref[raw] = envelope_by_hash[content_hash]


def _enrich_result(
    result: RetrievalResult, exposed_by_ref: dict[str, dict[str, Any]]
) -> RetrievalResult:
    refs = tuple(_enrich_ref(ref, exposed_by_ref) for ref in result.source_refs)
    return dataclasses.replace(result, source_refs=refs)


def _enrich_ref(ref: SourceRef, exposed_by_ref: dict[str, dict[str, Any]]) -> SourceRef:
    if ref.source_type != "chunk":
        return ref
    exposed = exposed_by_ref.get(ref.id)
    if not exposed:
        return ref
    # merge under "document" so chunk-local metadata (offsets) never collides
    # with a project attribute of the same name
    return dataclasses.replace(ref, metadata={**ref.metadata, "document": exposed})
