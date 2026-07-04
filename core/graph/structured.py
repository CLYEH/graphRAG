"""Structured extraction: rule-map tabular rows → entities/relations (DESIGN §5 step 3, §6, C3a).

The deterministic half of the graph step (§6: 結構化資料規則對映). Each
structured document ingested by C2 is one row (raw = canonical JSON, metadata
= ``{table, pk}``); a :class:`~core.graph.ontology.StructuredMapping` for that
table says which columns become entities and relations. No LLM — the mapping
is a rule, so the output is a pure function of the row and the mapping.

Identity is the frozen fingerprint (§27.3/§27.4), so this step is idempotent
and its output survives rebuilds:

- an entity is canonicalized by ``entity_key = fpv(norm(type)|norm(name)|disamb)``
  — two rows naming the same (type, name) collapse to ONE entity row (unique
  ``entities_by_key`` per build) carrying two mentions. The FUZZY merge of
  entities that DON'T share a normalized name (``USA`` vs ``United States``)
  is resolution's job (C4, §7), not this step's.
- a relation by ``relation_signature``; a piece of evidence by ``evidence_hash``.

Re-running converges (§5): existing entities/relations/mentions/evidence are
read back and reused, never duplicated — so a §27.7 wholesale retry only fills
what a prior run missed. Everything writes through the DR-006 build-scoped
writer. A row that can't be parsed is a ``failed`` item (§18) and the build
continues; a table with no mapping, or a row that maps to nothing, is
``skipped`` — both visible, neither silently dropped.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from core.graph.ontology import StructuredMapping
from core.observability.spec import ItemOutcome
from core.resolve import fingerprints
from core.stores import tables
from core.stores.repo import BuildScopedWriter

#: Rule-mapped graph elements are deterministic → full confidence, and their
#: provenance is a rule, not an LLM or a human (entities.created_by vocabulary).
_RULE_CONFIDENCE = 1.0
_CREATED_BY = "rule"

#: The mime the CSV/structured connector (C2) stamps on a tabular row.
_STRUCTURED_MIME = "application/json"


def row_source_ref(table: str, pk: str) -> str:
    """A LOSSLESS, splittable ``(table, pk)`` row ref (§27.2 cites table + pk).

    The table is length-prefixed so a ``:`` inside ``table`` or ``pk`` cannot
    collide two different pairs — ``("a:b", "c")`` and ``("a", "b:c")`` would
    both be ``"a:b:c"`` under naive joining, silently dropping the later row's
    mention/evidence (they dedup by this ref) — the same collision the
    fingerprint ``_join`` avoids. C6/C8 split it back into the contract's
    separate ``table`` and ``pk`` fields: read the integer up to the first
    ``:``, take that many chars as ``table``, the remainder (past one ``:``)
    is ``pk``.
    """
    return f"{len(table)}:{table}:{pk}"


@dataclass(frozen=True)
class GraphExtractReport:
    """The step result: rows written (new only) + the §18 item outcomes."""

    entities: int
    relations: int
    mentions: int
    evidence: int
    outcomes: tuple[ItemOutcome, ...]


class _BuildState:
    """In-memory image of the build's graph, seeded from what's already stored
    so a re-run reuses rows instead of duplicating them (§5). Every key is a
    frozen fingerprint, matching a DB unique index — the DB is the backstop,
    this is the fast path that also hands back the reusable row ids."""

    def __init__(self) -> None:
        self.entity_id_by_key: dict[str, uuid.UUID] = {}
        self.relation_id_by_sig: dict[str, uuid.UUID] = {}
        self.evidence_hashes: set[str] = set()
        self.mention_refs: set[tuple[uuid.UUID, str]] = set()

    async def preload(self, writer: BuildScopedWriter) -> None:
        for row in await writer.fetch_all(tables.entities):
            self.entity_id_by_key[row.entity_key] = row.id
        for row in await writer.fetch_all(tables.relations):
            if row.relation_signature is not None:
                self.relation_id_by_sig[row.relation_signature] = row.id
        for row in await writer.fetch_all(tables.relation_evidence):
            self.evidence_hashes.add(row.evidence_hash)
        self.mention_refs |= await writer.mention_refs()


async def extract_structured(
    writer: BuildScopedWriter, mappings: Mapping[str, StructuredMapping]
) -> GraphExtractReport:
    """Extract entities/relations from this build's structured documents.

    Reads the structured documents from the writer's own build, applies each
    table's mapping, and writes entities/mentions/relations/evidence — all
    deduped by fingerprint so the pass is idempotent (§5). ``mappings`` is
    keyed by table name; a document whose table has no mapping is skipped.

    The key and the mapping's own ``table`` must AGREE — the key routes
    documents by their ``metadata["table"]`` while ``mapping.table`` names the
    citation, so a mismatch (typo, stale copy) would cite rows under the wrong
    table, and two keys sharing one stale ``table`` would collapse to the same
    ``source_ref`` and silently drop mentions/evidence. A contradictory config
    is rejected before any row is read.
    """
    for key, declared in mappings.items():
        if key != declared.table:
            raise ValueError(
                f"mappings key {key!r} disagrees with its StructuredMapping.table "
                f"{declared.table!r} — the key selects documents by metadata table, "
                "the table names the §27.2 citation; a mismatch would cite rows "
                "under the wrong table"
            )
    state = _BuildState()
    await state.preload(writer)
    counts = {"entities": 0, "relations": 0, "mentions": 0, "evidence": 0}
    outcomes: list[ItemOutcome] = []

    for doc in await writer.fetch_all(
        tables.documents, tables.documents.c.mime == _STRUCTURED_MIME
    ):
        metadata = doc.metadata or {}
        table = metadata.get("table")
        mapping = mappings.get(table) if isinstance(table, str) else None
        if mapping is None:
            outcomes.append(ItemOutcome("document", doc.content_hash, "skipped"))
            continue
        # §27.2 row refs cite table + pk; the table half is guarded by the
        # mapping, the pk half must be present or the mention/evidence ref would
        # be an uncitable "table:" that still passes the source_ref<>'' CHECK.
        # C2 guards pk at ingest, so this is defensive — but fail loud (a failed
        # item), never mint half a citation.
        pk = _cell(metadata.get("pk"))
        if not pk:
            outcomes.append(ItemOutcome("document", doc.content_hash, "failed"))
            continue
        try:
            parsed = json.loads(doc.raw)
        except (json.JSONDecodeError, TypeError):
            outcomes.append(ItemOutcome("document", doc.content_hash, "failed"))
            continue

        source_ref = row_source_ref(mapping.table, pk)
        fields = parsed if isinstance(parsed, dict) else {}
        produced = await _extract_row(
            writer,
            mapping=mapping,
            fields=fields,
            source_ref=source_ref,
            state=state,
            counts=counts,
        )
        outcomes.append(
            ItemOutcome("document", doc.content_hash, "extracted" if produced else "skipped")
        )

    return GraphExtractReport(
        counts["entities"],
        counts["relations"],
        counts["mentions"],
        counts["evidence"],
        tuple(outcomes),
    )


async def _extract_row(
    writer: BuildScopedWriter,
    *,
    mapping: StructuredMapping,
    fields: dict[str, object],
    source_ref: str,
    state: _BuildState,
    counts: dict[str, int],
) -> bool:
    """Map one row to graph elements. Returns True if it produced any entity."""
    now = datetime.now(tz=UTC)
    row_keys: dict[str, str] = {}  # alias -> entity_key for THIS row's entities

    for alias, rule in mapping.entities.items():
        name = _cell(fields.get(rule.name_column))
        if not name:
            continue  # this entity is absent for this row; relations to it skip
        disamb = (
            _cell(fields.get(rule.disambiguator_column))
            if rule.disambiguator_column is not None
            else ""
        )
        key = fingerprints.entity_key(rule.entity_type, name, disamb or None)
        row_keys[alias] = key
        if key not in state.entity_id_by_key:
            entity_id = uuid.uuid4()
            await writer.insert(
                tables.entities,
                id=entity_id,
                type=rule.entity_type,
                canonical_name=name,
                entity_key=key,
                attributes={},
                status="active",
                review_status="unreviewed",
                created_by=_CREATED_BY,
                created_at=now,
                updated_at=now,
            )
            state.entity_id_by_key[key] = entity_id
            counts["entities"] += 1

        entity_id = state.entity_id_by_key[key]
        if (entity_id, source_ref) not in state.mention_refs:
            await writer.insert_entity_mention(
                entity_id=entity_id,
                source_kind="structured",
                source_ref=source_ref,
                surface_form=name,
                confidence=_RULE_CONFIDENCE,
            )
            state.mention_refs.add((entity_id, source_ref))
            counts["mentions"] += 1

    for relation in mapping.relations:
        src_key = row_keys.get(relation.src)
        dst_key = row_keys.get(relation.dst)
        if src_key is None or dst_key is None:
            continue  # an endpoint entity was absent for this row
        signature = fingerprints.relation_signature(src_key, relation.relation_type, dst_key)
        relation_id = state.relation_id_by_sig.get(signature)
        if relation_id is None:
            relation_id = uuid.uuid4()
            await writer.insert(
                tables.relations,
                id=relation_id,
                src_entity_id=state.entity_id_by_key[src_key],
                dst_entity_id=state.entity_id_by_key[dst_key],
                type=relation.relation_type,
                attributes={},
                relation_signature=signature,
                status="active",
                review_status="unreviewed",
                created_by=_CREATED_BY,
                confidence=_RULE_CONFIDENCE,
                created_at=now,
                updated_at=now,
            )
            state.relation_id_by_sig[signature] = relation_id
            counts["relations"] += 1

        digest = fingerprints.evidence_hash(signature, source_ref, None)
        if digest not in state.evidence_hashes:
            await writer.insert(
                tables.relation_evidence,
                id=uuid.uuid4(),
                relation_id=relation_id,
                evidence_type="row",
                evidence_ref=source_ref,
                evidence_hash=digest,
                confidence=_RULE_CONFIDENCE,
                created_at=now,
            )
            state.evidence_hashes.add(digest)
            counts["evidence"] += 1

    return bool(row_keys)


def _cell(value: object) -> str:
    """A row cell as a trimmed string (blank/absent → "")."""
    if value is None:
        return ""
    return str(value).strip()
