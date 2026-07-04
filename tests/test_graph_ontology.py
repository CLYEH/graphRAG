"""Why: the structured mapping is a rule set applied blindly to every row of a
build (§6). A relation naming an entity alias that doesn't exist, or a blank
type/column, is a configuration bug that must fail at construction — before it
silently drops relations or raises a KeyError mid-build, halfway through
writing a graph.
"""

from __future__ import annotations

import pytest

from core.graph.ontology import EntityRule, RelationRule, StructuredMapping


def test_valid_mapping_constructs() -> None:
    mapping = StructuredMapping(
        table="people",
        entities={
            "person": EntityRule("Person", "name", disambiguator_column="id"),
            "company": EntityRule("Company", "employer"),
        },
        relations=(RelationRule("WORKS_AT", src="person", dst="company"),),
    )
    assert mapping.entities["person"].disambiguator_column == "id"
    assert mapping.relations[0].relation_type == "WORKS_AT"


def test_relation_referencing_unknown_alias_is_rejected() -> None:
    """The blank/uncitable-at-the-door principle extended to references: a
    relation whose endpoint alias isn't defined can never form an edge."""
    with pytest.raises(ValueError, match="references entity alias 'manager'"):
        StructuredMapping(
            table="people",
            entities={"person": EntityRule("Person", "name")},
            relations=(RelationRule("REPORTS_TO", src="person", dst="manager"),),
        )


def test_entity_rule_rejects_blank_type_or_column() -> None:
    with pytest.raises(ValueError, match="entity_type"):
        EntityRule("  ", "name")
    with pytest.raises(ValueError, match="name_column"):
        EntityRule("Person", "")
    with pytest.raises(ValueError, match="disambiguator_column"):
        EntityRule("Person", "name", disambiguator_column="  ")


def test_relation_rule_rejects_blank_type() -> None:
    with pytest.raises(ValueError, match="relation_type"):
        RelationRule("", src="a", dst="b")


def test_mapping_rejects_blank_table_or_no_entities() -> None:
    with pytest.raises(ValueError, match="table"):
        StructuredMapping(table=" ", entities={"p": EntityRule("Person", "name")})
    with pytest.raises(ValueError, match="no entities"):
        StructuredMapping(table="people", entities={})
