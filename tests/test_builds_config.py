"""Why: ``load_build_config`` is the seam between free-form ``projects.config``
JSONB (user-controlled, API round-tripped) and the typed config the §5 stages
run against. Its job is the layer the typed dataclasses don't own — pulling
blocks out by key and type-checking leaves — while DELEGATING business rules to
those dataclasses (so the rules stay single-sourced). These tests pin: absent
blocks take defaults; a PRESENT-but-broken block fails loud (never collapses to
the absent behavior); every leaf is type-checked (a bool is not an int); the
dataclasses' own ``__post_init__`` failures surface as one ``BuildConfigError``;
and the ``table``-inside-a-mapping footgun is unrepresentable.
"""

from __future__ import annotations

import pytest

from core.builds.config import BuildConfig, BuildConfigError, load_build_config
from core.clean.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP
from core.graph.ontology import EntityRule, RelationRule, TextOntology
from core.resolve.resolution import ResolutionConfig

_FULL = {
    "ontology": {
        "entity_types": ["Person", "Company"],
        "relation_types": ["WORKS_AT"],
        "proposal_policy": "auto",
    },
    "structured_mappings": {
        "companies": {
            "entities": {
                "person": {"entity_type": "Person", "name_column": "name"},
                "company": {
                    "entity_type": "Company",
                    "name_column": "co",
                    "disambiguator_column": "id",
                },
            },
            "relations": [{"relation_type": "WORKS_AT", "src": "person", "dst": "company"}],
        }
    },
    "resolution": {
        "auto_merge_threshold": 0.9,
        "review_threshold": 0.6,
        "embedding_weight": 0.3,
        "carry_review": False,
    },
    "chunking": {"max_chars": 800, "overlap": 100},
}


def test_full_config_parses_into_typed_objects() -> None:
    cfg = load_build_config(_FULL)
    assert isinstance(cfg, BuildConfig)
    assert cfg.ontology == TextOntology(("Person", "Company"), ("WORKS_AT",))
    assert cfg.ontology_proposal_policy == "auto"
    assert cfg.chunk_max_chars == 800 and cfg.chunk_overlap == 100
    assert cfg.resolution == ResolutionConfig(
        auto_merge_threshold=0.9, review_threshold=0.6, embedding_weight=0.3, carry_review=False
    )
    mapping = cfg.structured_mappings["companies"]
    assert mapping.table == "companies"  # the KEY becomes the table name
    assert mapping.entities["company"] == EntityRule("Company", "co", "id")
    assert mapping.entities["person"] == EntityRule("Person", "name")
    assert mapping.relations == (RelationRule("WORKS_AT", "person", "company"),)


def test_empty_config_is_all_defaults() -> None:
    # a project with no build config yet is loadable — no ontology, no mappings,
    # default resolution/chunking; runnability then depends on its sources.
    cfg = load_build_config({})
    assert cfg.ontology is None
    assert cfg.ontology_proposal_policy == "review"  # safe default: hold proposals
    assert cfg.structured_mappings == {}
    assert cfg.resolution == ResolutionConfig()
    assert (cfg.chunk_max_chars, cfg.chunk_overlap) == (DEFAULT_MAX_CHARS, DEFAULT_OVERLAP)


def test_absent_ontology_is_none_not_an_error() -> None:
    cfg = load_build_config({"structured_mappings": {}})
    assert cfg.ontology is None


def test_present_but_incomplete_ontology_fails_loud() -> None:
    # the whole point: an OMITTED ontology and a BROKEN one must not collapse to
    # the same behavior — a broken block is a config bug, not "no ontology".
    with pytest.raises(BuildConfigError, match="no relation types"):
        load_build_config({"ontology": {"entity_types": ["Person"]}})
    with pytest.raises(BuildConfigError, match="no entity types"):
        load_build_config({"ontology": {"entity_types": [], "relation_types": ["R"]}})


def test_blank_ontology_type_is_rejected() -> None:
    with pytest.raises(BuildConfigError, match="non-empty"):
        load_build_config({"ontology": {"entity_types": ["  "], "relation_types": ["R"]}})


@pytest.mark.parametrize("policy", ["review", "auto"])
def test_valid_proposal_policy_is_carried(policy: str) -> None:
    cfg = load_build_config(
        {"ontology": {"entity_types": ["E"], "relation_types": ["R"], "proposal_policy": policy}}
    )
    assert cfg.ontology_proposal_policy == policy


def test_unknown_proposal_policy_fails_loud() -> None:
    # a typo'd policy must not silently behave like a real one.
    with pytest.raises(BuildConfigError, match="proposal_policy"):
        load_build_config(
            {
                "ontology": {
                    "entity_types": ["E"],
                    "relation_types": ["R"],
                    "proposal_policy": "auto ",
                }
            }
        )


def test_bool_is_not_accepted_as_a_number_or_int() -> None:
    # isinstance(True, int) is True in Python — the loader must reject it, else
    # `true` silently becomes 1 (a real config-value bug).
    with pytest.raises(BuildConfigError, match="chunking.max_chars must be an integer"):
        load_build_config({"chunking": {"max_chars": True}})
    with pytest.raises(BuildConfigError, match="embedding_weight must be a number"):
        load_build_config({"resolution": {"embedding_weight": False}})


def test_wrong_leaf_types_are_rejected_with_a_path() -> None:
    with pytest.raises(BuildConfigError, match="ontology.entity_types must be an array"):
        load_build_config({"ontology": {"entity_types": "Person", "relation_types": ["R"]}})
    with pytest.raises(BuildConfigError, match=r"entity_types\[0\] must be a string"):
        load_build_config({"ontology": {"entity_types": [1], "relation_types": ["R"]}})
    with pytest.raises(BuildConfigError, match="resolution must be an object"):
        load_build_config({"resolution": []})
    with pytest.raises(BuildConfigError, match="carry_review must be a boolean"):
        load_build_config({"resolution": {"carry_review": "yes"}})


def test_resolution_threshold_ordering_is_delegated_and_wrapped() -> None:
    # the business rule lives in ResolutionConfig.__post_init__; the loader
    # re-wraps its ValueError as BuildConfigError so callers catch one type.
    with pytest.raises(BuildConfigError, match="thresholds must satisfy"):
        load_build_config({"resolution": {"review_threshold": 0.9, "auto_merge_threshold": 0.5}})


def test_resolution_partial_override_keeps_other_defaults() -> None:
    cfg = load_build_config({"resolution": {"embedding_weight": 0.4}})
    assert cfg.resolution == ResolutionConfig(embedding_weight=0.4)  # thresholds still defaulted


def test_structured_mapping_relation_endpoint_must_reference_a_defined_alias() -> None:
    # delegated to StructuredMapping.__post_init__, surfaced as BuildConfigError.
    bad = {
        "structured_mappings": {
            "t": {
                "entities": {"a": {"entity_type": "E", "name_column": "n"}},
                "relations": [{"relation_type": "R", "src": "a", "dst": "missing"}],
            }
        }
    }
    with pytest.raises(BuildConfigError, match="references entity alias 'missing'"):
        load_build_config(bad)


def test_structured_mapping_rejects_a_redundant_table_field() -> None:
    # the mapping KEY is the table name; a second 'table' value could disagree —
    # made unrepresentable, not merely re-checked (the composite-identifier lesson).
    bad = {
        "structured_mappings": {
            "companies": {
                "table": "other",
                "entities": {"a": {"entity_type": "E", "name_column": "n"}},
            }
        }
    }
    with pytest.raises(BuildConfigError, match="must not carry a 'table' field"):
        load_build_config(bad)


def test_entity_rule_requires_type_and_name_column() -> None:
    with pytest.raises(BuildConfigError, match="entity_type is required"):
        load_build_config({"structured_mappings": {"t": {"entities": {"a": {"name_column": "n"}}}}})
    with pytest.raises(BuildConfigError, match="name_column is required"):
        load_build_config({"structured_mappings": {"t": {"entities": {"a": {"entity_type": "E"}}}}})


def test_relation_rule_requires_all_three_keys() -> None:
    with pytest.raises(BuildConfigError, match=r"relations\[0\].dst is required"):
        load_build_config(
            {
                "structured_mappings": {
                    "t": {
                        "entities": {"a": {"entity_type": "E", "name_column": "n"}},
                        "relations": [{"relation_type": "R", "src": "a"}],
                    }
                }
            }
        )


def test_relations_must_be_an_array() -> None:
    with pytest.raises(BuildConfigError, match="relations must be an array"):
        load_build_config(
            {
                "structured_mappings": {
                    "t": {
                        "entities": {"a": {"entity_type": "E", "name_column": "n"}},
                        "relations": {},
                    }
                }
            }
        )


def test_nested_leaf_type_error_is_reported_once_not_double_prefixed() -> None:
    # a wrong-typed leaf inside a rule surfaces with its own path exactly once,
    # not re-wrapped by the dataclass-construction error prefix.
    with pytest.raises(BuildConfigError) as exc:
        load_build_config(
            {
                "structured_mappings": {
                    "t": {"entities": {"a": {"entity_type": 1, "name_column": "n"}}}
                }
            }
        )
    message = str(exc.value)
    assert message == "structured_mappings.t.entities.a.entity_type must be a string, got int"


def test_unknown_top_level_keys_are_ignored() -> None:
    # projects.config is free-form (API round-trips it verbatim) — the loader
    # reads the keys it knows and ignores the rest, rather than rejecting a
    # config that legitimately carries other project settings.
    cfg = load_build_config({"display_name": "x", "chunking": {"max_chars": 500, "overlap": 50}})
    assert (cfg.chunk_max_chars, cfg.chunk_overlap) == (500, 50)


def test_non_object_config_is_rejected() -> None:
    with pytest.raises(BuildConfigError, match="config must be an object"):
        load_build_config([])  # type: ignore[arg-type]
