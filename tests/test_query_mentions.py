"""Why: core/query/mentions.py is the ONE seam that turns stored entity
mention rows into v1.1 resolvable refs for every emitting surface (semantic,
graph, get_entity). What must hold: the stored ``chunk:{content_hash}:{ordinal}``
string parses strictly (a corrupt SoR string reads as uncitable, never a
crash), a mention that cannot satisfy the v1.1 shape is DROPPED and counted
(§22 over-drop — a dead or half-cited ref must never be emitted), quotes are
capped at the contract's 512, and the whole page costs two batched reads.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from core.query.mentions import parse_chunk_mention_ref, resolved_mention_refs
from core.stores.repo import BuildScopedRepo


class _FakeRepo:
    def __init__(
        self,
        mentions: dict[uuid.UUID, list[tuple[str, str, str | None]]],
        chunks: dict[tuple[str, int], SimpleNamespace] | None = None,
    ) -> None:
        self.mentions = mentions
        self.chunks = chunks or {}
        self.queries = 0

    async def mentions_by_entity(
        self, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, str, str | None]]]:
        self.queries += 1
        return {eid: rows for eid, rows in self.mentions.items() if eid in entity_ids}

    async def chunks_by_content_ref(self, refs: Any) -> dict[tuple[str, int], SimpleNamespace]:
        self.queries += 1
        return {key: row for key, row in self.chunks.items() if key in set(refs)}


def _chunk_row(chunk_id: uuid.UUID | None = None, *, uri: str = "s3://d.md") -> SimpleNamespace:
    return SimpleNamespace(
        id=chunk_id or uuid.uuid4(),
        ordinal=0,
        start_offset=3,
        end_offset=9,
        content_hash="aa11bb22cc",
        source_uri=uri,
    )


def test_the_stored_ref_parses_strictly() -> None:
    """The stored form is the writer's ``chunk:{fixed-width-hex}:{ordinal}``;
    anything else — the truncated, the prefixless, the non-hex — must read as
    uncitable (None), never raise: stored refs are SoR data but a corrupt row
    cannot be allowed to fail the whole query (§22)."""
    assert parse_chunk_mention_ref("chunk:aa11bb22cc:7") == ("aa11bb22cc", 7)
    assert parse_chunk_mention_ref("chunk:AA11BB22CC:7") is None  # not lower-hex
    assert parse_chunk_mention_ref("chunk:short:0") is None  # non-hex / too short
    assert parse_chunk_mention_ref("chunk-abc123") is None
    assert parse_chunk_mention_ref("chunk:aa11bb22cc:") is None
    # Codex #127 r2: the ordinal is bounded at 9 digits — an unbounded match
    # let int() raise past CPython's digit limit and >2^31-1 fail at the PG
    # INTEGER bind: an error instead of the promised uncitable drop
    assert parse_chunk_mention_ref("chunk:aa11bb22cc:1234567890") is None
    assert parse_chunk_mention_ref("chunk:aa11bb22cc:" + "9" * 5000) is None
    assert parse_chunk_mention_ref("chunk:aa11bb22cc:999999999") == ("aa11bb22cc", 999999999)
    assert parse_chunk_mention_ref("") is None
    assert parse_chunk_mention_ref(cast(Any, None)) is None


async def test_resolvable_mentions_become_v11_refs_and_the_rest_drop() -> None:
    """The v1.1 promise: an emitted chunk mention ref carries the chunk UUID
    (get_chunk-ready) + source_uri + quote + offsets — and every mention that
    CANNOT satisfy that shape (drifted chunk, NULL quote, unsplittable row
    ref, unknown kind) is dropped and counted rather than emitted as the
    v1.0-era dead citation."""
    cited, uncitable = uuid.uuid4(), uuid.uuid4()
    chunk_id = uuid.uuid4()
    repo = _FakeRepo(
        mentions={
            cited: [
                ("text", "chunk:aa11bb22cc:0", "quoted surface"),
                ("structured", "1:t:42", None),
            ],
            uncitable: [
                ("text", "chunk:ffffffffff:0", "quote"),  # no backing chunk (drift)
                ("text", "chunk:aa11bb22cc:0", None),  # NULL surface_form — no quote
                ("structured", "t:42", None),  # not length-prefixed — unsplittable
                ("weird", "x", "q"),  # out-of-vocabulary kind
            ],
        },
        chunks={("aa11bb22cc", 0): _chunk_row(chunk_id)},
    )
    refs_by_entity, dropped, capped = await resolved_mention_refs(
        cast(BuildScopedRepo, repo), [cited, uncitable]
    )

    assert capped == set()
    # deterministic order: sorted by ref id, NOT DB mention order (#34)
    first, second = refs_by_entity[cited]
    chunk_ref, row_ref = sorted(refs_by_entity[cited], key=lambda r: r.source_type)
    assert [first.id, second.id] == sorted([first.id, second.id])
    assert chunk_ref.source_type == "chunk" and chunk_ref.id == str(chunk_id)
    assert chunk_ref.source_uri == "s3://d.md"
    assert chunk_ref.metadata == {"quote": "quoted surface", "start_offset": 3, "end_offset": 9}
    assert row_ref.source_type == "row" and row_ref.metadata == {"table": "t", "pk": "42"}

    assert uncitable not in refs_by_entity  # zero resolvable mentions → absent
    assert dropped == {uncitable: 4}  # per-entity provenance (page-exact emitters)
    assert repo.queries == 2  # one mention read + one chunk read for the page


async def test_the_quote_is_capped_at_the_contract_512() -> None:
    """v1.1 caps quote at 512 (the relation-evidence precedent): a mention
    whose surface form exceeds it must emit the truncated quote, not a
    schema-invalid ref and not a drop — the citation is still real."""
    entity_id = uuid.uuid4()
    repo = _FakeRepo(
        mentions={entity_id: [("text", "chunk:aa11bb22cc:0", "長" * 600)]},
        chunks={("aa11bb22cc", 0): _chunk_row()},
    )
    refs_by_entity, dropped, _ = await resolved_mention_refs(
        cast(BuildScopedRepo, repo), [entity_id]
    )
    assert dropped == {}
    quote = refs_by_entity[entity_id][0].metadata["quote"]
    assert len(quote) == 512


async def test_result_refs_cap_at_eight_and_get_entity_stays_uncapped() -> None:
    """The MCP3 refs-cap precedent applied to mentions (measured: a 70-mention
    entity would emit 70 rich refs per result): §16 emitters cap at
    MENTION_REFS_CAP with a deterministic (id-sorted) prefix, and the
    ESCAPE HATCH is real — cap=None (the get_entity path) returns the full
    list, so "see get_entity" is never a dead pointer (#124)."""
    from core.query.mentions import MENTION_REFS_CAP

    entity_id = uuid.uuid4()
    chunks = {(f"aa11bb22c{i:x}", 0): _chunk_row() for i in range(10)}
    for key, row in chunks.items():
        row.content_hash = key[0]
    repo = _FakeRepo(
        mentions={entity_id: [("text", f"chunk:{h}:0", "q") for h, _ in chunks]},
        chunks=chunks,
    )
    capped_refs, _, capped_count = await resolved_mention_refs(
        cast(BuildScopedRepo, repo), [entity_id]
    )
    assert len(capped_refs[entity_id]) == MENTION_REFS_CAP
    assert capped_count == {entity_id}
    assert [r.id for r in capped_refs[entity_id]] == sorted(r.id for r in capped_refs[entity_id])

    full_refs, _, uncapped_count = await resolved_mention_refs(
        cast(BuildScopedRepo, repo), [entity_id], cap=None
    )
    assert len(full_refs[entity_id]) == 10  # introspection = full membership
    assert uncapped_count == set()
