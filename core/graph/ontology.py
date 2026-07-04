"""Structured-source ontology: rule-mapping tabular rows to graph elements (DESIGN §6, C3a).

§6 splits extraction by source kind: structured data is mapped by RULE
(deterministic, no LLM — this module), documents by schema-guided LLM
extraction (C3b). A :class:`StructuredMapping` describes, for ONE source
table, which columns become entities of which type and which relations
connect them.

Passed in as typed objects, not loaded from YAML: project config-file loading
is BA1's job. C3a needs only the shape — the way C2 took chunking tunables as
arguments until config lands.

The rules are validated at construction (a relation naming an entity alias
that doesn't exist, or a blank type/column, is a configuration bug that must
fail loudly before any extraction writes rows — not surface later as a
KeyError mid-build).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EntityRule:
    """One entity minted per row: its ``entity_type`` and the columns that
    give its name and (optionally) a stable external id.

    ``disambiguator_column`` names a column holding a stable external id
    (§27.3) — typically the row's pk — used to tell apart two real entities
    that share a name. Absent/blank values fall back to name-only identity
    (see :func:`core.resolve.fingerprints.entity_key`).
    """

    entity_type: str
    name_column: str
    disambiguator_column: str | None = None

    def __post_init__(self) -> None:
        if not self.entity_type.strip():
            raise ValueError("EntityRule.entity_type must be non-empty")
        if not self.name_column.strip():
            raise ValueError("EntityRule.name_column must be non-empty")
        if self.disambiguator_column is not None and not self.disambiguator_column.strip():
            raise ValueError(
                "EntityRule.disambiguator_column must be a non-empty column name or None"
            )


@dataclass(frozen=True)
class RelationRule:
    """A directed relation between two entity aliases of the same row."""

    relation_type: str
    src: str  # an alias key of StructuredMapping.entities
    dst: str

    def __post_init__(self) -> None:
        if not self.relation_type.strip():
            raise ValueError("RelationRule.relation_type must be non-empty")


@dataclass(frozen=True)
class TextOntology:
    """The schema that GUIDES document extraction (§6: 受 schema 引導抽取).

    Entity/relation types the LLM is allowed to emit. Types outside this
    vocabulary are not written — they are held out as proposals for the §6
    待審池 (storage lands with the proposal-pool slice); silently accepting
    them would let one hallucinated type mint arbitrary graph vocabulary.
    """

    entity_types: tuple[str, ...]
    relation_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.entity_types:
            raise ValueError("TextOntology defines no entity types")
        if not self.relation_types:
            raise ValueError("TextOntology defines no relation types")
        for label, values in (("entity", self.entity_types), ("relation", self.relation_types)):
            for value in values:
                if not value.strip():
                    raise ValueError(f"TextOntology {label} types must be non-empty")


@dataclass(frozen=True)
class StructuredMapping:
    """How one source ``table`` maps to entities + relations (§6)."""

    table: str
    entities: Mapping[str, EntityRule]
    relations: tuple[RelationRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.table.strip():
            raise ValueError("StructuredMapping.table must be non-empty")
        if not self.entities:
            raise ValueError(f"StructuredMapping for table {self.table!r} defines no entities")
        for relation in self.relations:
            for endpoint in (relation.src, relation.dst):
                if endpoint not in self.entities:
                    raise ValueError(
                        f"relation {relation.relation_type!r} of table {self.table!r} "
                        f"references entity alias {endpoint!r}, which is not defined "
                        f"(defined: {sorted(self.entities)})"
                    )
