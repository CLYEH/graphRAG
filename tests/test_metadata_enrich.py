"""Why: enrich-on-read is where DR-010 rule 6 (document metadata resolved from
the SoR at query time, not duplicated per chunk) and rule 7 (exposure by
allowlist, never by presence) actually protect a query response. These tests
pin: a chunk source_ref gains ONLY the allowlisted slice of its document's
envelope, merged under ``document`` so chunk-local offsets never collide; an
empty allowlist leaves the response untouched (fail-closed — a governance field
in storage is not exposed); a non-chunk ref is never touched; and a chunk whose
document has no metadata degrades to unchanged (no crash, no empty branch).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from core.metadata.schema import MetadataExposure
from core.query.metadata_enrich import enrich_response_metadata
from core.query.results import McpResponse, RetrievalResult, SourceRef
from core.stores import tables
from core.stores.repo import BuildScopedRepo

_CHUNK = uuid.uuid4()
_DOC = uuid.uuid4()
_ENVELOPE = {
    "schema_version": "1.0",
    "system": {"connector": "upload", "original_filename": "case.txt"},
    "context": {
        "title": "Ruling 42",
        "document_type": "ruling",
        "attributes": {"case_number": "42"},
    },
    "governance": {"visibility": "restricted"},
}


class _FakeRepo:
    """A build-scoped repo stub: answers the chunk and document bulk reads the
    enricher makes, keyed by the table object it is handed."""

    def __init__(self, chunk_rows: list[Any], document_rows: list[Any]) -> None:
        self._chunks = chunk_rows
        self._docs = document_rows
        self.build_id = uuid.uuid4()
        self.project = "demo"

    async def fetch_all(self, table: Any, *where: Any) -> list[Any]:
        if table is tables.chunks:
            return self._chunks
        if table is tables.documents:
            return self._docs
        return []


def _response(ref: SourceRef) -> McpResponse:
    return McpResponse(
        query="q",
        tool="semantic_search",
        project="demo",
        build_id=str(uuid.uuid4()),
        results=(
            RetrievalResult(result_type="chunk", id=str(_CHUNK), score=1.0, source_refs=(ref,)),
        ),
    )


def _chunk_ref() -> SourceRef:
    return SourceRef(
        source_type="chunk",
        id=str(_CHUNK),
        source_uri="file:///c.txt",
        metadata={"start_offset": 0, "end_offset": 5},
    )


def _repo(metadata: Any = _ENVELOPE) -> BuildScopedRepo:
    return cast(
        BuildScopedRepo,
        _FakeRepo(
            chunk_rows=[SimpleNamespace(id=_CHUNK, document_id=_DOC)],
            document_rows=[SimpleNamespace(id=_DOC, metadata=metadata)],
        ),
    )


async def test_chunk_ref_gains_only_allowlisted_document_metadata() -> None:
    exposure = MetadataExposure(fields=("context.title", "context.attributes.case_number"))
    enriched = await enrich_response_metadata(_response(_chunk_ref()), _repo(), exposure)
    ref = enriched.results[0].source_refs[0]
    # chunk-local metadata preserved, document metadata merged under "document"
    assert ref.metadata["start_offset"] == 0 and ref.metadata["end_offset"] == 5
    assert ref.metadata["document"] == {
        "context": {"title": "Ruling 42", "attributes": {"case_number": "42"}}
    }
    # governance was in the envelope but NOT allowlisted → never reaches the ref
    assert "governance" not in ref.metadata["document"]


async def test_stable_chunk_ref_resolves_via_document_content_hash() -> None:
    """Entity/graph hits cite chunks by their STABLE ref (``chunk:<content_hash>:
    <ordinal>`` — ``core.graph.documents.chunk_source_ref``), NOT a ``chunks.id``
    UUID. The enricher must resolve those through ``documents.content_hash`` — else
    allowlisted document metadata never surfaces on entity results (the common
    text-backed hit), and rule-6 enrichment silently does nothing for them."""
    content_hash = "a" * 64
    stable_ref = f"chunk:{content_hash}:3"
    exposure = MetadataExposure(fields=("context.title",))
    ref = SourceRef(
        source_type="chunk",
        id=stable_ref,
        source_uri="file:///c.txt",
        metadata={"start_offset": 0, "end_offset": 5},
    )
    # a repo with NO chunk rows (the UUID path finds nothing) but a document whose
    # content_hash matches the stable ref — only the stable-ref path can resolve it
    repo = cast(
        BuildScopedRepo,
        _FakeRepo(
            chunk_rows=[],
            document_rows=[SimpleNamespace(id=_DOC, content_hash=content_hash, metadata=_ENVELOPE)],
        ),
    )
    enriched = await enrich_response_metadata(_response(ref), repo, exposure)
    got = enriched.results[0].source_refs[0]
    assert got.metadata["document"] == {"context": {"title": "Ruling 42"}}
    assert got.metadata["start_offset"] == 0  # chunk-local metadata preserved


async def test_empty_allowlist_leaves_response_unchanged() -> None:
    response = _response(_chunk_ref())
    enriched = await enrich_response_metadata(response, _repo(), MetadataExposure(fields=()))
    assert enriched is response  # fail-closed: no read, no change


async def test_non_chunk_ref_is_untouched() -> None:
    row_ref = SourceRef(source_type="row", id="t:1", metadata={"table": "t", "pk": "1"})
    exposure = MetadataExposure(fields=("context.title",))
    enriched = await enrich_response_metadata(_response(row_ref), _repo(), exposure)
    assert enriched.results[0].source_refs[0].metadata == {"table": "t", "pk": "1"}


async def test_document_without_metadata_degrades_to_unchanged() -> None:
    exposure = MetadataExposure(fields=("context.title",))
    original = _response(_chunk_ref())
    enriched = await enrich_response_metadata(original, _repo(metadata=None), exposure)
    # no document metadata to expose → the ref keeps only its chunk-local keys
    assert "document" not in enriched.results[0].source_refs[0].metadata
