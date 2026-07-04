"""Why: document extraction is where UNTRUSTED model output meets the frozen
provenance contract. §27.4 chunk evidence must carry a verbatim, locatable
quote (offsets into the original text) — so a paraphrased quote must cost the
relation, not mint fake evidence; §6 schema guidance means out-of-ontology
types become visible proposals, never silently stored vocabulary; and the two
extraction halves must share identity, so a rule-extracted and an
LLM-extracted "Acme" are ONE entity. All of it must stay idempotent (§5).

The LLM is faked (canned JSON per chunk) and the writer is the same in-memory
fake as C3a's tests: what's under test is the acceptance/refusal logic, not
the network or the database (live writes: the integration file).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from llama_index.core.llms import LLM, ChatMessage, ChatResponse

from core.graph.documents import TextExtractReport, chunk_source_ref, extract_documents
from core.graph.ontology import TextOntology
from core.resolve import fingerprints
from core.stores import tables
from core.stores.repo import BuildScopedWriter

_ONTOLOGY = TextOntology(entity_types=("Person", "Company"), relation_types=("WORKS_AT",))


class _FakeLLM:
    """Answers each chunk from a canned {chunk_text: answer} table."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.calls = 0

    async def achat(self, messages: list[ChatMessage], **_: Any) -> ChatResponse:
        self.calls += 1
        chunk_text = str(messages[-1].content)
        return ChatResponse(
            message=ChatMessage(role="assistant", content=self._answers[chunk_text])
        )


class _FakeWriter:
    """In-memory BuildScopedWriter double (same shape as C3a's tests)."""

    def __init__(self, docs: list[SimpleNamespace], chunks: list[SimpleNamespace]) -> None:
        self._docs = docs
        self._chunks = chunks
        self.entities: list[dict[str, Any]] = []
        self.relations: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.mentions: list[dict[str, Any]] = []

    async def fetch_all(self, table: Any, *_where: Any) -> list[SimpleNamespace]:
        if table is tables.documents:
            return list(self._docs)
        if table is tables.chunks:
            # extract_documents filters by document_id predicate; the fake
            # serves all chunks and relies on per-doc chunk lists being scoped
            return list(self._chunks)
        store = {
            tables.entities: self.entities,
            tables.relations: self.relations,
            tables.relation_evidence: self.evidence,
        }[table]
        return [SimpleNamespace(**row) for row in store]

    async def mention_refs(self) -> set[tuple[Any, str]]:
        return {(m["entity_id"], m["source_ref"]) for m in self.mentions}

    async def insert(self, table: Any, /, **values: Any) -> None:
        {
            tables.entities: self.entities,
            tables.relations: self.relations,
            tables.relation_evidence: self.evidence,
        }[table].append(values)

    async def insert_entity_mention(self, **kwargs: Any) -> None:
        self.mentions.append(kwargs)


def _doc(ch: str = "hash-a") -> SimpleNamespace:
    return SimpleNamespace(id="doc-1", mime="text/plain", content_hash=ch, source_uri="mem://a.txt")


def _chunk(text: str, *, ordinal: int = 0, start: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"chunk-{ordinal}",
        ordinal=ordinal,
        text=text,
        start_offset=start,
        end_offset=start + len(text),
        document_id="doc-1",
    )


def _answer(entities: list[dict[str, Any]], relations: list[dict[str, Any]]) -> str:
    return json.dumps({"entities": entities, "relations": relations})


async def _extract(writer: _FakeWriter, llm: _FakeLLM) -> TextExtractReport:
    return await extract_documents(cast(BuildScopedWriter, writer), cast(LLM, llm), _ONTOLOGY)


_TEXT = "Alice moved to Berlin. Alice works at Acme since 2019."


def _good_answer() -> str:
    return _answer(
        [
            {"type": "Person", "name": "Alice", "confidence": 0.9},
            {"type": "Company", "name": "Acme", "confidence": 0.8},
        ],
        [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Company",
                "dst_name": "Acme",
                "quote": "Alice works at Acme",
                "confidence": 0.7,
            }
        ],
    )


async def test_accepted_extractions_land_with_contract_grade_evidence() -> None:
    """The full accept path: frozen fingerprints, created_by='llm', text
    mention with the chunk ref, and §27.4 evidence — verbatim quote with
    DOCUMENT-absolute offsets that slice the original text exactly."""
    chunk = _chunk(_TEXT, start=100)  # non-zero start proves the offset frame
    writer = _FakeWriter([_doc()], [chunk])
    report = await _extract(writer, _FakeLLM({_TEXT: _good_answer()}))

    assert (report.entities, report.relations, report.mentions, report.evidence) == (2, 1, 2, 1)
    assert report.proposals == () and report.discarded == ()
    assert [o.status for o in report.outcomes] == ["extracted"]

    alice_key = fingerprints.entity_key("Person", "Alice")
    assert {e["entity_key"] for e in writer.entities} == {
        alice_key,
        fingerprints.entity_key("Company", "Acme"),
    }
    assert all(e["created_by"] == "llm" for e in writer.entities)
    ref = chunk_source_ref("hash-a", 0)
    assert all(m["source_kind"] == "text" and m["source_ref"] == ref for m in writer.mentions)

    ev = writer.evidence[0]
    quote = "Alice works at Acme"
    match = _TEXT.find(quote)
    assert ev["evidence_type"] == "chunk"
    assert ev["start_offset"] == 100 + match
    assert ev["end_offset"] == 100 + match + len(quote)
    assert ev["quote"] == quote and ev["source_uri"] == "mem://a.txt"
    assert _TEXT[ev["start_offset"] - 100 : ev["end_offset"] - 100] == quote


async def test_out_of_ontology_types_become_proposals_not_rows() -> None:
    """§6: unknown types are held out for ontology review — visible in the
    report, absent from storage; a relation leaning on the held-out entity
    loses its endpoint and is discarded with the reason."""
    answer = _answer(
        [
            {"type": "Spaceship", "name": "Rocinante", "confidence": 0.9},
            {"type": "Person", "name": "Alice", "confidence": 0.9},
        ],
        [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "PILOTS",  # unknown relation type
                "dst_type": "Spaceship",
                "dst_name": "Rocinante",
                "quote": "irrelevant",
                "confidence": 0.5,
            },
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Spaceship",
                "dst_name": "Rocinante",  # held-out endpoint
                "quote": "irrelevant",
                "confidence": 0.5,
            },
        ],
    )
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))

    assert {(p.kind, p.type_name) for p in report.proposals} == {
        ("entity", "Spaceship"),
        ("relation", "PILOTS"),
    }
    assert len(writer.entities) == 1  # only Alice
    assert writer.relations == []
    assert any("endpoint" in d.reason for d in report.discarded)


async def test_blank_types_and_names_are_discarded_with_reasons() -> None:
    """Every Discarded reason path is a real branch: an entity missing its
    type/name and a relation missing its type are refused visibly — a blank
    would otherwise mint an empty-key fingerprint or an untyped edge."""
    answer = _answer(
        [
            {"type": "  ", "name": "Alice", "confidence": 0.9},
            {"type": "Person", "name": "", "confidence": 0.9},
            {"type": "Person", "name": "Bob", "confidence": 0.9},
        ],
        [
            {
                "src_type": "Person",
                "src_name": "Bob",
                "type": "",
                "dst_type": "Person",
                "dst_name": "Bob",
                "quote": "irrelevant",
                "confidence": 0.5,
            }
        ],
    )
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))
    assert len(writer.entities) == 1  # only Bob
    assert writer.relations == []
    reasons = [d.reason for d in report.discarded]
    assert sum("entity missing type/name" in r for r in reasons) == 2
    assert sum("relation missing type" in r for r in reasons) == 1


async def test_blank_quotes_never_mint_evidence() -> None:
    """A whitespace-only quote is truthy, find(" ") matches almost any text,
    and " " passes the DB's quote <> '' CHECK — so without this guard a
    relation could store blank 'evidence' with no auditable span. Also holds
    for the pathological quote whose first 512 chars (the stored prefix) are
    all whitespace."""
    pathological = " " * 600 + "x"
    text = f"Alice works at Acme.{pathological}"
    for bad_quote in (" ", "\n\t ", pathological):
        answer = _answer(
            [
                {"type": "Person", "name": "Alice", "confidence": 0.9},
                {"type": "Company", "name": "Acme", "confidence": 0.8},
            ],
            [
                {
                    "src_type": "Person",
                    "src_name": "Alice",
                    "type": "WORKS_AT",
                    "dst_type": "Company",
                    "dst_name": "Acme",
                    "quote": bad_quote,
                    "confidence": 0.7,
                }
            ],
        )
        writer = _FakeWriter(
            [_doc()],
            [
                SimpleNamespace(
                    id="c",
                    ordinal=0,
                    text=text,
                    start_offset=0,
                    end_offset=len(text),
                    document_id="doc-1",
                )
            ],
        )
        report = await _extract(writer, _FakeLLM({text: answer}))
        assert writer.relations == [] and writer.evidence == [], bad_quote
        assert any("quote is blank" in d.reason for d in report.discarded), bad_quote


async def test_unlocatable_quote_costs_the_relation_not_the_contract() -> None:
    """§27.4: chunk evidence must be a locatable verbatim span. A paraphrased
    quote cannot be cited, and a relation without evidence would violate the
    §27.2 provenance minimum — so the relation is discarded, visibly."""
    answer = _answer(
        [
            {"type": "Person", "name": "Alice", "confidence": 0.9},
            {"type": "Company", "name": "Acme", "confidence": 0.8},
        ],
        [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Company",
                "dst_name": "Acme",
                "quote": "Alice is employed by Acme",  # paraphrase, not in text
                "confidence": 0.7,
            }
        ],
    )
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))
    assert report.relations == 0 and report.evidence == 0
    assert writer.relations == [] and writer.evidence == []
    assert any("quote not found" in d.reason for d in report.discarded)


async def test_malformed_answer_fails_the_document_and_the_build_continues() -> None:
    """§22: an LLM/parse failure marks THAT document failed; later documents
    still extract."""
    bad_doc = SimpleNamespace(
        id="doc-1", mime="text/plain", content_hash="hash-bad", source_uri="mem://bad.txt"
    )
    good_doc = SimpleNamespace(
        id="doc-2", mime="text/plain", content_hash="hash-good", source_uri="mem://good.txt"
    )
    bad_chunk = SimpleNamespace(
        id="c-1", ordinal=0, text="bad text", start_offset=0, end_offset=8, document_id="doc-1"
    )
    good_chunk = SimpleNamespace(
        id="c-2", ordinal=0, text=_TEXT, start_offset=0, end_offset=len(_TEXT), document_id="doc-2"
    )

    class _PerDocWriter(_FakeWriter):
        async def fetch_all(self, table: Any, *where: Any) -> list[SimpleNamespace]:
            if table is tables.chunks:
                # crude per-document routing driven by call order is fragile;
                # match on the predicate's bound value instead
                bound = where[0].right.value if where else None
                return [c for c in self._chunks if c.document_id == bound]
            return await super().fetch_all(table, *where)

    writer = _PerDocWriter([bad_doc, good_doc], [bad_chunk, good_chunk])
    report = await _extract(
        writer, _FakeLLM({"bad text": "NOT JSON AT ALL", _TEXT: _good_answer()})
    )
    statuses = {o.item_ref: o.status for o in report.outcomes}
    assert statuses == {"hash-bad": "failed", "hash-good": "extracted"}
    assert report.entities == 2  # the good document still landed


async def test_wrong_shaped_fields_fail_the_document_not_the_pass() -> None:
    """Valid JSON with a non-list field ({"entities": 1}) must count as a
    malformed answer for THAT document — before this guard, iterating it
    raised outside the failure boundary and one bad chunk aborted the whole
    extraction pass instead of §22's fail-the-item-and-continue."""
    bad_doc = SimpleNamespace(
        id="doc-1", mime="text/plain", content_hash="hash-bad", source_uri="mem://bad.txt"
    )
    good_doc = SimpleNamespace(
        id="doc-2", mime="text/plain", content_hash="hash-good", source_uri="mem://good.txt"
    )
    bad_chunk = SimpleNamespace(
        id="c-1", ordinal=0, text="bad text", start_offset=0, end_offset=8, document_id="doc-1"
    )
    good_chunk = SimpleNamespace(
        id="c-2", ordinal=0, text=_TEXT, start_offset=0, end_offset=len(_TEXT), document_id="doc-2"
    )

    class _PerDocWriter(_FakeWriter):
        async def fetch_all(self, table: Any, *where: Any) -> list[SimpleNamespace]:
            if table is tables.chunks:
                bound = where[0].right.value if where else None
                return [c for c in self._chunks if c.document_id == bound]
            return await super().fetch_all(table, *where)

    writer = _PerDocWriter([bad_doc, good_doc], [bad_chunk, good_chunk])
    report = await _extract(
        writer,
        _FakeLLM(
            {
                "bad text": json.dumps({"entities": 1, "relations": []}),
                _TEXT: _good_answer(),
            }
        ),
    )
    statuses = {o.item_ref: o.status for o in report.outcomes}
    assert statuses == {"hash-bad": "failed", "hash-good": "extracted"}
    assert report.entities == 2  # the pass continued past the bad document


async def test_omitted_answer_arrays_fail_the_document_not_skip_it() -> None:
    """The prompt demands BOTH arrays ("shaped exactly"); `{}` or a single
    field is a schema-violating answer, not "found nothing". If it read as
    empty output the document would record `skipped` — and §27.7
    retry-failed-only (which re-runs only `failed`) would never retry it, so
    a degraded model run would hide as a permanently under-extracted build.
    A model that finds nothing must say so with explicit empty lists."""
    for bad in ("{}", json.dumps({"entities": []}), json.dumps({"relations": []})):
        writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
        report = await _extract(writer, _FakeLLM({_TEXT: bad}))
        assert [o.status for o in report.outcomes] == ["failed"], bad
    # the explicit nothing-found answer is a legitimate skip, not a failure
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(
        writer, _FakeLLM({_TEXT: json.dumps({"entities": [], "relations": []})})
    )
    assert [o.status for o in report.outcomes] == ["skipped"]


async def test_non_string_scalars_are_discarded_never_repr_minted() -> None:
    """The untrusted boundary's leaf level: str() coercion would turn a
    non-string name ({"text": "Alice"}) into its Python repr and PERSIST it
    as a canonical_name + fingerprint — garbage entities polluting the graph.
    Every identity/evidence-bearing scalar must BE a string or the item is
    discarded visibly."""
    answer = json.dumps(
        {
            "entities": [
                {"type": "Person", "name": {"text": "Alice"}, "confidence": 0.9},
                {"type": ["Person"], "name": "Bob", "confidence": 0.9},
                {"type": "Person", "name": "Carol", "confidence": 0.9},
            ],
            "relations": [
                {
                    "src_type": "Person",
                    "src_name": "Carol",
                    "type": "WORKS_AT",
                    "dst_type": "Person",
                    "dst_name": "Carol",
                    "quote": 42,  # non-string evidence
                    "confidence": 0.5,
                }
            ],
        }
    )
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))
    assert [e["canonical_name"] for e in writer.entities] == ["Carol"]  # no reprs minted
    assert writer.relations == []
    reasons = [d.reason for d in report.discarded]
    assert sum("entity type/name is not a string" in r for r in reasons) == 2
    assert sum("relation field is not a string" in r for r in reasons) == 1


async def test_non_object_list_items_are_discarded_visibly() -> None:
    """A list whose items aren't objects is half-right model output — each
    bad item is a visible Discarded, and the good items still land."""
    answer = json.dumps(
        {
            "entities": ["banana", {"type": "Person", "name": "Alice", "confidence": 0.9}],
            "relations": [42],
        }
    )
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))
    assert len(writer.entities) == 1  # Alice landed
    reasons = [d.reason for d in report.discarded]
    assert sum("entity item is not an object" in r for r in reasons) == 1
    assert sum("relation item is not an object" in r for r in reasons) == 1


async def test_llm_and_rule_sources_share_identity() -> None:
    """The hybrid promise (§6): an entity already minted by structured
    extraction is REUSED — no duplicate row, one more mention from 'text'."""
    existing_key = fingerprints.entity_key("Company", "Acme")
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    writer.entities.append({"id": "pre-existing", "entity_key": existing_key, "type": "Company"})
    report = await _extract(writer, _FakeLLM({_TEXT: _good_answer()}))
    assert report.entities == 1  # only Alice is new
    assert len(writer.entities) == 2  # Acme not duplicated
    acme_mentions = [m for m in writer.mentions if m["entity_id"] == "pre-existing"]
    assert len(acme_mentions) == 1 and acme_mentions[0]["source_kind"] == "text"


async def test_rerun_converges_and_writes_nothing_new() -> None:
    writer = _FakeWriter([_doc()], [_chunk(_TEXT)])
    llm = _FakeLLM({_TEXT: _good_answer()})
    first = await _extract(writer, llm)
    second = await _extract(writer, llm)
    assert (first.entities, first.relations, first.mentions, first.evidence) == (2, 1, 2, 1)
    assert (second.entities, second.relations, second.mentions, second.evidence) == (0, 0, 0, 0)


async def test_long_quote_is_stored_truncated_with_full_span_offsets() -> None:
    """§27.4 caps the STORED quote at 512 (存摘句); the span offsets keep the
    real extent so the citation still addresses the full evidence."""
    long_quote = "Alice works at Acme " * 30  # 600 chars
    text = f"Intro. {long_quote}End."
    answer = _answer(
        [
            {"type": "Person", "name": "Alice", "confidence": 0.9},
            {"type": "Company", "name": "Acme", "confidence": 0.8},
        ],
        [
            {
                "src_type": "Person",
                "src_name": "Alice",
                "type": "WORKS_AT",
                "dst_type": "Company",
                "dst_name": "Acme",
                "quote": long_quote,
                "confidence": 0.7,
            }
        ],
    )
    writer = _FakeWriter(
        [_doc()],
        [
            SimpleNamespace(
                id="c",
                ordinal=0,
                text=text,
                start_offset=0,
                end_offset=len(text),
                document_id="doc-1",
            )
        ],
    )
    report = await _extract(writer, _FakeLLM({text: answer}))
    assert report.evidence == 1
    ev = writer.evidence[0]
    assert len(ev["quote"]) == 512
    assert ev["end_offset"] - ev["start_offset"] == len(long_quote)


async def test_confidence_is_clamped_and_structured_docs_are_not_touched() -> None:
    structured = SimpleNamespace(
        id="doc-s", mime="application/json", content_hash="hash-s", source_uri="mem://s"
    )
    answer = _answer([{"type": "Person", "name": "Alice", "confidence": 7}], [])
    writer = _FakeWriter([_doc(), structured], [_chunk(_TEXT)])
    report = await _extract(writer, _FakeLLM({_TEXT: answer}))
    assert writer.mentions[0]["confidence"] == 1.0
    # the structured doc yields NO outcome here — it is C3a's work item
    assert [o.item_ref for o in report.outcomes] == ["hash-a"]
