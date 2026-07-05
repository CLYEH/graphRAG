"""Why: semantic_search's job is to turn Qdrant hits into §16 results that CITE
their source — the decisions that matter (enrich chunk hits with uri+offsets,
entity hits with a mention ref, DROP a hit that can't be cited rather than emit
it uncited, refuse mismatched scopes, order deterministically) are logic that
holds independent of the live stores. The kNN itself and the payload filter are
proven against real Qdrant in the C1d/C5 integration tests and reused here via
fakes, which is also where fast-suite coverage of semantic.py comes from
(integration tests are excluded from the coverage gate).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest

from core.query.results import McpResponse
from core.query.semantic import semantic_search
from core.stores import tables
from core.stores.repo import BuildScopedRepo
from core.stores.vectors import BuildScopedVectorRepo

_PROJECT = "p1"
_BUILD = uuid.uuid4()

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "contracts" / "mcp_response.schema.json").read_text(
        encoding="utf-8"
    )
)
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


class _FakeRepo:
    """In-memory BuildScopedRepo: the two bulk reads semantic uses plus the
    mention lookup. Predicates are ignored — the code filters by the dict
    lookups that follow, and the test controls the store contents."""

    def __init__(self, project: str = _PROJECT, build_id: uuid.UUID = _BUILD) -> None:
        self.project = project
        self.build_id = build_id
        self.rows: dict[Any, list[dict[str, Any]]] = {tables.chunks: [], tables.documents: []}
        self.mentions: dict[uuid.UUID, list[tuple[str, str]]] = {}

    async def fetch_all(self, table: Any, *where: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(**row) for row in self.rows[table]]

    async def mentions_by_entity(
        self, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, str]]]:
        return {eid: self.mentions[eid] for eid in entity_ids if eid in self.mentions}


class _FakeVectors:
    def __init__(
        self, hits: list[SimpleNamespace], project: str = _PROJECT, build_id: uuid.UUID = _BUILD
    ) -> None:
        self.project = project
        self.build_id = build_id
        self._hits = hits
        self.searched_limit: int | None = None

    async def search(
        self, vector: list[float], limit: int, point_type: str | None = None
    ) -> list[SimpleNamespace]:
        self.searched_limit = limit
        return self._hits[:limit]


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def aget_text_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2, 0.3]


def _chunk_hit(chunk_id: uuid.UUID, *, score: float, text: str = "a chunk") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(chunk_id),
        score=score,
        payload={
            "canonical_id": str(chunk_id),
            "type": "chunk",
            "text": text,
            "chunk_id": str(chunk_id),
            "project": _PROJECT,
            "build_id": str(_BUILD),
        },
    )


def _entity_hit(entity_id: uuid.UUID, *, score: float, name: str = "Acme") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(entity_id),
        score=score,
        payload={
            "canonical_id": str(entity_id),
            "type": "entity",
            "text": name,
            "entity_id": str(entity_id),
            "project": _PROJECT,
            "build_id": str(_BUILD),
        },
    )


def _add_chunk(repo: _FakeRepo, chunk_id: uuid.UUID, *, uri: str = "s3://d.md") -> None:
    doc_id = uuid.uuid4()
    repo.rows[tables.documents].append({"id": doc_id, "source_uri": uri})
    repo.rows[tables.chunks].append(
        {"id": chunk_id, "document_id": doc_id, "start_offset": 5, "end_offset": 12}
    )


async def _run(repo: _FakeRepo, vectors: _FakeVectors, top_k: int = 10) -> McpResponse:
    return await semantic_search(
        cast(BuildScopedRepo, repo),
        cast(BuildScopedVectorRepo, vectors),
        cast("Any", _FakeEmbedder()),
        "who owns onboarding?",
        top_k,
    )


async def test_enriches_chunk_and_entity_hits_into_valid_contract_response() -> None:
    """The happy path: a chunk hit gains uri + offsets, an entity hit gains its
    mention ref, and the whole envelope validates against the frozen §16 schema
    — ordered score desc, bound to the build it read."""
    chunk_id, entity_id = uuid.uuid4(), uuid.uuid4()
    repo = _FakeRepo()
    _add_chunk(repo, chunk_id, uri="s3://onboarding.md")
    repo.mentions[entity_id] = [("text", "chunk:h1:0")]
    vectors = _FakeVectors([_chunk_hit(chunk_id, score=0.9), _entity_hit(entity_id, score=0.7)])

    response = await _run(repo, vectors)
    payload = response.to_dict()
    _VALIDATOR.validate(payload)

    assert payload["tool"] == "semantic_search" and payload["build_id"] == str(_BUILD)
    assert [r["result_type"] for r in payload["results"]] == ["chunk", "entity"]  # score order
    chunk = payload["results"][0]
    assert chunk["source_refs"][0] == {
        "source_type": "chunk",
        "id": str(chunk_id),
        "source_uri": "s3://onboarding.md",
        "metadata": {"start_offset": 5, "end_offset": 12},
    }
    entity = payload["results"][1]
    assert entity["title"] == "Acme"
    assert entity["source_refs"] == [{"source_type": "chunk", "id": "chunk:h1:0"}]
    assert payload["warnings"] == []


async def test_query_is_embedded_and_top_k_bounds_the_search() -> None:
    """The query text is embedded (not passed as text to Qdrant) and top_k is
    the kNN limit — the two facts a caller relies on to reason about cost."""
    embedder = _FakeEmbedder()
    chunk_id = uuid.uuid4()
    repo = _FakeRepo()
    _add_chunk(repo, chunk_id)
    vectors = _FakeVectors([_chunk_hit(chunk_id, score=0.5)])
    await semantic_search(
        cast(BuildScopedRepo, repo),
        cast(BuildScopedVectorRepo, vectors),
        cast("Any", embedder),
        "find it",
        top_k=3,
    )
    assert embedder.calls == ["find it"]
    assert vectors.searched_limit == 3


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
async def test_an_out_of_contract_top_k_degrades_typed(bad: Any) -> None:
    """§22 sibling parity (the C6d/C6e door guard): a non-positive, bool, or
    non-int top_k must come back as a typed GUARDRAIL_BLOCKED — never reach
    Qdrant as an invalid limit and error the tool (the contract minimum is 1)."""
    embedder = _FakeEmbedder()
    vectors = _FakeVectors([])
    response = await semantic_search(
        cast(BuildScopedRepo, _FakeRepo()),
        cast(BuildScopedVectorRepo, vectors),
        cast("Any", embedder),
        "find it",
        top_k=bad,
    )
    assert response.results == ()
    assert [w.code for w in response.warnings] == ["GUARDRAIL_BLOCKED"]
    assert embedder.calls == [] and vectors.searched_limit is None  # nothing reached the store


async def test_entity_mention_kind_maps_to_the_citable_source_type() -> None:
    """§27.2: a `text` mention is a chunk citation, a `structured` mention is a
    row citation — the source_kind is the only thing that tells them apart."""
    text_ent, row_ent = uuid.uuid4(), uuid.uuid4()
    repo = _FakeRepo()
    repo.mentions[text_ent] = [("text", "chunk:h:0")]
    repo.mentions[row_ent] = [("structured", "9:employees:7")]
    vectors = _FakeVectors([_entity_hit(text_ent, score=0.9), _entity_hit(row_ent, score=0.8)])
    payload = (await _run(repo, vectors)).to_dict()
    _VALIDATOR.validate(payload)
    kinds = {r["source_refs"][0]["source_type"] for r in payload["results"]}
    assert kinds == {"chunk", "row"}


async def test_uncitable_hits_are_dropped_with_a_typed_warning() -> None:
    """require_sources bites at retrieval too: a chunk hit whose row is gone
    (drift), and an entity hit with no surviving mention, cannot be cited — so
    they are DROPPED, not emitted uncited, and the omission is a typed
    PARTIAL_RESULTS warning (§22), never silent."""
    good_chunk, ghost_chunk = uuid.uuid4(), uuid.uuid4()
    mentionless = uuid.uuid4()
    repo = _FakeRepo()
    _add_chunk(repo, good_chunk)  # ghost_chunk intentionally absent from Postgres
    vectors = _FakeVectors(
        [
            _chunk_hit(good_chunk, score=0.9),
            _chunk_hit(ghost_chunk, score=0.8),  # no row → dropped
            _entity_hit(mentionless, score=0.7),  # no mention → dropped
        ]
    )
    response = await _run(repo, vectors)
    assert [r.result_type for r in response.results] == ["chunk"]
    assert [r.id for r in response.results] == [str(good_chunk)]
    assert len(response.warnings) == 1
    assert response.warnings[0].code == "PARTIAL_RESULTS"
    assert "2 hit(s)" in response.warnings[0].message
    _VALIDATOR.validate(response.to_dict())


async def test_a_chunk_whose_document_uri_is_missing_is_dropped() -> None:
    """A chunk row with no resolvable document source_uri cannot satisfy the
    §27.2 chunk minimum (uri is mandatory), so it drops rather than emitting a
    ref the schema would reject."""
    orphan = uuid.uuid4()
    repo = _FakeRepo()
    # chunk row exists but points at a document not in the store → no uri
    repo.rows[tables.chunks].append(
        {"id": orphan, "document_id": uuid.uuid4(), "start_offset": 0, "end_offset": 3}
    )
    vectors = _FakeVectors([_chunk_hit(orphan, score=0.9)])
    response = await _run(repo, vectors)
    assert response.results == ()
    assert response.warnings[0].code == "PARTIAL_RESULTS"


async def test_mismatched_repo_and_vector_scopes_are_refused() -> None:
    """Enrichment reads Postgres for the build the vector hit came from — if the
    two readers bind different builds, a hit would be cited with another build's
    rows (a DR-006 cross-scope leak), so the mismatch is refused up front."""
    repo = _FakeRepo(build_id=_BUILD)
    vectors = _FakeVectors([], build_id=uuid.uuid4())
    with pytest.raises(ValueError, match="scopes disagree"):
        await _run(repo, vectors)


async def test_empty_hits_yield_an_empty_but_valid_response() -> None:
    """No hits is a valid answer, not an error: an empty result list still
    produces a schema-valid, build-scoped envelope with no warnings."""
    response = await _run(_FakeRepo(), _FakeVectors([]))
    payload = response.to_dict()
    _VALIDATOR.validate(payload)
    assert payload["results"] == [] and payload["warnings"] == []


async def test_malformed_or_unmappable_hits_are_dropped_not_crashed_on() -> None:
    """Qdrant payloads are untrusted at query time (drift, a future point type
    C6a doesn't handle yet, a payload-less hit): each such hit is DROPPED with a
    warning, never a KeyError that fails the whole query. Covers the defensive
    branches a well-formed build never exercises."""
    no_payload = SimpleNamespace(id="x", score=0.9, payload=None)
    unknown_type = SimpleNamespace(
        id="y", score=0.8, payload={"type": "relation", "canonical_id": "r"}
    )
    chunk_without_id = SimpleNamespace(id="z", score=0.7, payload={"type": "chunk", "text": "t"})
    entity_without_id = SimpleNamespace(id="w", score=0.6, payload={"type": "entity", "text": "E"})
    # a corrupt projection row with a non-UUID id must DROP (not raise): one bad
    # row cannot fail the whole query (§22) — the exact P2 Codex flagged
    chunk_bad_uuid = SimpleNamespace(
        id="v", score=0.5, payload={"type": "chunk", "chunk_id": "not-a-uuid", "text": "t"}
    )
    entity_bad_uuid = SimpleNamespace(
        id="u", score=0.4, payload={"type": "entity", "entity_id": "also bad", "text": "E"}
    )
    vectors = _FakeVectors(
        [
            no_payload,
            unknown_type,
            chunk_without_id,
            entity_without_id,
            chunk_bad_uuid,
            entity_bad_uuid,
        ]
    )
    response = await _run(_FakeRepo(), vectors)
    assert response.results == ()
    assert response.warnings[0].code == "PARTIAL_RESULTS"
    assert "6 hit(s)" in response.warnings[0].message


async def test_corrupt_payload_display_or_id_never_makes_the_response_invalid() -> None:
    """A citable hit with a corrupt (non-string) `canonical_id` or `text` must
    NOT poison the response: the id comes from the VALIDATED uuid (not the raw
    payload), and a non-string text/title is coerced to None. The hit stays —
    it's still citable — and the payload stays schema-valid, so one corrupt row
    can't invalidate the whole answer (the exact P2 Codex flagged)."""
    entity_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    repo = _FakeRepo()
    _add_chunk(repo, chunk_id)
    repo.mentions[entity_id] = [("text", "chunk:h:0")]
    # entity: numeric canonical_id + object text; chunk: numeric text
    entity_hit = SimpleNamespace(
        id="e",
        score=0.9,
        payload={"type": "entity", "entity_id": str(entity_id), "canonical_id": 42, "text": {}},
    )
    chunk_hit = SimpleNamespace(
        id="c",
        score=0.8,
        payload={"type": "chunk", "chunk_id": str(chunk_id), "text": 99},
    )
    response = await _run(repo, _FakeVectors([entity_hit, chunk_hit]))
    payload = response.to_dict()
    _VALIDATOR.validate(payload)  # would fail if a non-string id/text leaked through
    by_type = {r["result_type"]: r for r in payload["results"]}
    assert by_type["entity"]["id"] == str(entity_id)  # validated uuid, not 42
    assert by_type["entity"]["title"] is None  # corrupt object text → None
    assert by_type["chunk"]["text"] is None  # corrupt numeric text → None
    assert response.warnings == ()  # both hits kept — they are citable
