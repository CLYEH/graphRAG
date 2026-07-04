"""Why: structured extraction is the deterministic graph step (§6) — its output
must be a pure, idempotent function of the rows and the mapping. The identity
rules are load-bearing: two rows naming the same (type, name) MUST collapse to
one entity (§27.3 exact-key canonicalization, the precondition C4's fuzzy merge
builds on), a disambiguator MUST split genuine namesakes, and a re-run MUST
converge (§5) — write nothing twice. These are checked against the frozen
fingerprints, not re-derived, so a drift in either surfaces here.

The writer is faked (in-memory) so the extraction LOGIC is tested without a
database; the live DR-006 writes, constraints and cross-build isolation are
covered in test_graph_structured_integration.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from core.graph.ontology import EntityRule, RelationRule, StructuredMapping
from core.graph.structured import GraphExtractReport, extract_structured
from core.resolve import fingerprints
from core.stores import tables
from core.stores.repo import BuildScopedWriter


class _FakeWriter:
    """Captures inserts and serves documents, mimicking BuildScopedWriter's
    surface used by structured extraction. Inserts persist on the instance, so
    calling extract_structured twice exercises real cross-run idempotency."""

    def __init__(self, docs: list[SimpleNamespace]) -> None:
        self._docs = docs
        self.entities: list[dict[str, Any]] = []
        self.relations: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.mentions: list[dict[str, Any]] = []

    async def fetch_all(self, table: Any, *_where: Any) -> list[SimpleNamespace]:
        if table is tables.documents:
            return list(self._docs)
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


async def _extract(
    writer: _FakeWriter, mappings: dict[str, StructuredMapping]
) -> GraphExtractReport:
    return await extract_structured(cast(BuildScopedWriter, writer), mappings)


def _doc(raw: str, *, table: str | None, pk: str = "1", ch: str | None = None) -> SimpleNamespace:
    metadata = {"pk": pk}
    if table is not None:
        metadata["table"] = table
    return SimpleNamespace(
        raw=raw,
        metadata=metadata,
        mime="application/json",
        content_hash=ch or f"h-{table}-{pk}",
        source_uri=f"mem://{table}/{pk}",
    )


def _row(**cols: str) -> str:
    return json.dumps(cols, sort_keys=True)


_PERSON_COMPANY = {
    "people": StructuredMapping(
        table="people",
        entities={
            "person": EntityRule("Person", "name"),
            "company": EntityRule("Company", "employer"),
        },
        relations=(RelationRule("WORKS_AT", src="person", dst="company"),),
    )
}


async def test_same_type_name_collapses_to_one_entity_with_two_mentions() -> None:
    """Exact-key canonicalization: two rows naming Person 'Alice' become ONE
    entity (the entities_by_key unique index) carrying two mentions."""
    writer = _FakeWriter(
        [
            _doc(_row(name="Alice", employer="Acme"), table="people", pk="1"),
            _doc(_row(name="Alice", employer="Acme"), table="people", pk="2"),
        ]
    )
    report = await _extract(writer, {"people": _PERSON_COMPANY["people"]})

    # Alice + Acme = 2 distinct entities, each mentioned in both rows
    assert report.entities == 2
    assert {e["entity_key"] for e in writer.entities} == {
        fingerprints.entity_key("Person", "Alice"),
        fingerprints.entity_key("Company", "Acme"),
    }
    assert report.mentions == 4  # 2 entities x 2 rows
    assert [o.status for o in report.outcomes] == ["extracted", "extracted"]


async def test_relation_and_evidence_carry_frozen_signatures() -> None:
    writer = _FakeWriter([_doc(_row(name="Alice", employer="Acme"), table="people", pk="7")])
    report = await _extract(writer, _PERSON_COMPANY)

    assert report.relations == 1 and report.evidence == 1
    sig = fingerprints.relation_signature(
        fingerprints.entity_key("Person", "Alice"),
        "WORKS_AT",
        fingerprints.entity_key("Company", "Acme"),
    )
    assert writer.relations[0]["relation_signature"] == sig
    assert writer.relations[0]["created_by"] == "rule"
    assert writer.evidence[0]["evidence_type"] == "row"
    assert writer.evidence[0]["evidence_ref"] == "people:7"
    assert writer.evidence[0]["evidence_hash"] == fingerprints.evidence_hash(sig, "people:7", None)


async def test_disambiguator_splits_namesakes() -> None:
    """Two people both named 'Alice' but with distinct external ids are two
    entities — the disambiguator is exactly what keeps them apart (§27.3)."""
    mapping = {
        "people": StructuredMapping(
            table="people",
            entities={"person": EntityRule("Person", "name", disambiguator_column="id")},
        )
    }
    writer = _FakeWriter(
        [
            _doc(_row(name="Alice", id="1"), table="people", pk="1"),
            _doc(_row(name="Alice", id="2"), table="people", pk="2"),
        ]
    )
    report = await _extract(writer, mapping)
    assert report.entities == 2


async def test_unmapped_table_is_skipped_not_dropped() -> None:
    writer = _FakeWriter([_doc(_row(name="x"), table="orders", pk="1")])
    report = await _extract(writer, _PERSON_COMPANY)
    assert report.entities == 0
    assert [o.status for o in report.outcomes] == ["skipped"]


async def test_missing_pk_is_a_failed_item_not_a_half_citation() -> None:
    """§27.2 cites table + pk; a table-mapped row without a pk would mint an
    uncitable 'table:' ref that still passes the source_ref<>'' CHECK — fail
    loud (Rule 12) instead of half a citation. C2 guards pk at ingest, so this
    is defensive, but the door stays closed here too."""
    writer = _FakeWriter([_doc(_row(name="Alice", employer="Acme"), table="people", pk="")])
    report = await _extract(writer, _PERSON_COMPANY)
    assert report.entities == 0
    assert [o.status for o in report.outcomes] == ["failed"]


async def test_unparseable_row_is_a_failed_item_and_build_continues() -> None:
    writer = _FakeWriter(
        [
            _doc("{ this is not json", table="people", pk="1", ch="bad"),
            _doc(_row(name="Bob", employer="Acme"), table="people", pk="2", ch="good"),
        ]
    )
    report = await _extract(writer, _PERSON_COMPANY)
    statuses = {o.item_ref: o.status for o in report.outcomes}
    assert statuses == {"bad": "failed", "good": "extracted"}
    assert report.entities == 2  # the good row still produced its graph


async def test_blank_name_leaves_entity_absent_and_skips_its_relations() -> None:
    """A row missing the company name yields only the person; the WORKS_AT
    relation has no endpoint and is skipped rather than half-formed."""
    writer = _FakeWriter([_doc(_row(name="Alice", employer=""), table="people", pk="1")])
    report = await _extract(writer, _PERSON_COMPANY)
    assert report.entities == 1 and report.relations == 0
    assert [o.status for o in report.outcomes] == ["extracted"]


async def test_rerun_converges_and_writes_nothing_new() -> None:
    """§5 idempotency: a wholesale retry re-reads the stored graph and reuses
    it — no duplicate entities, mentions, relations or evidence."""
    docs = [
        _doc(_row(name="Alice", employer="Acme"), table="people", pk="1"),
        _doc(_row(name="Alice", employer="Acme"), table="people", pk="2"),
    ]
    writer = _FakeWriter(docs)
    first = await _extract(writer, _PERSON_COMPANY)
    second = await _extract(writer, _PERSON_COMPANY)

    assert (first.entities, first.relations, first.mentions, first.evidence) == (2, 1, 4, 2)
    assert (second.entities, second.relations, second.mentions, second.evidence) == (0, 0, 0, 0)
    assert len(writer.entities) == 2 and len(writer.relations) == 1
    assert len(writer.mentions) == 4 and len(writer.evidence) == 2


async def test_one_relation_many_rows_gives_one_edge_many_evidence() -> None:
    """The same (src, type, dst) from two rows is one relation with two pieces
    of evidence — the edge dedups by signature, evidence by (ref)."""
    writer = _FakeWriter(
        [
            _doc(_row(name="Alice", employer="Acme"), table="people", pk="1"),
            _doc(_row(name="Alice", employer="Acme"), table="people", pk="2"),
        ]
    )
    report = await _extract(writer, _PERSON_COMPANY)
    assert report.relations == 1
    assert report.evidence == 2
    assert {e["evidence_ref"] for e in writer.evidence} == {"people:1", "people:2"}
