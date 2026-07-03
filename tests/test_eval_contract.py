"""Contract tests — contracts/golden.schema.json (Track 0 P4, DESIGN §20/§27.5).

golden.yaml is the *input* that gates activation (§14 preflight): if the schema
under-rejects, a typo'd or empty expectation silently never runs and the eval
gate turns false-green; if it over-rejects, legitimate golden sets can't be
written. These tests pin both directions, plus the enum lockstep with the other
two frozen contracts (DR-002).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import yaml

from core.eval.spec import EXPECTS_FIELDS

pytestmark = pytest.mark.contract

_CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
_GOLDEN_SCHEMA = _CONTRACTS / "golden.schema.json"
_MCP_SCHEMA = _CONTRACTS / "mcp_response.schema.json"
_OPENAPI = _CONTRACTS / "openapi.yaml"


@pytest.fixture(scope="module")
def golden_schema() -> dict[str, Any]:
    assert _GOLDEN_SCHEMA.exists(), (
        "contracts/golden.schema.json is the frozen Track 0 P4 deliverable"
    )
    return cast(dict[str, Any], json.loads(_GOLDEN_SCHEMA.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def golden_validator(golden_schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    # format_checker makes "format": "regex" enforcing — without it a broken
    # answer_regex would only surface at eval runtime, not at authoring time.
    return jsonschema.Draft202012Validator(
        golden_schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def test_golden_schema_is_valid(golden_schema: dict[str, Any]) -> None:
    """The frozen deliverable must be a valid Draft 2020-12 schema."""
    jsonschema.Draft202012Validator.check_schema(golden_schema)


def test_golden_schema_version_is_frozen(golden_schema: dict[str, Any]) -> None:
    """DR-002: schema_version pins the contract; only a breaking change bumps it."""
    assert golden_schema["properties"]["schema_version"]["const"] == "1.0"
    assert "schema_version" in golden_schema["required"]


def test_query_mode_stays_in_lockstep_with_other_contracts(golden_schema: dict[str, Any]) -> None:
    """A golden case exercises one of the five frozen retrieval modes — mode drift
    between the three contract artifacts would let golden sets name modes the
    query surface doesn't have (or miss modes it does)."""
    golden_modes = set(golden_schema["$defs"]["QueryMode"]["enum"])
    mcp = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    api = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    assert golden_modes == set(mcp["$defs"]["QueryMode"]["enum"])
    assert golden_modes == set(api["components"]["schemas"]["QueryMode"]["enum"])


def test_expects_vocabulary_matches_core_spec(golden_schema: dict[str, Any]) -> None:
    """core.eval.spec.EXPECTS_FIELDS mirrors the schema's Expects block — the
    schema file is the machine-checked truth, and Python-side consumers (C10)
    key off the tuple, so the two must never drift apart."""
    schema_fields = set(golden_schema["$defs"]["Expects"]["properties"])
    assert schema_fields == set(EXPECTS_FIELDS)


# The canonical golden set is authored as YAML (§20: eval/golden.yaml), so the
# example goes through yaml.safe_load exactly like the real artifact will.
_VALID_GOLDEN_YAML = """
schema_version: "1.0"
cases:
  - question: "Who owns the onboarding process?"
    mode: hybrid
    expects:
      must_contain_entities: ["People Ops", "Onboarding"]
      must_cite_sources: ["s3://acme/docs/onboarding.md"]
      answer_regex: "People Ops"
      must_include_relations:
        - { src: "People Ops", type: "OWNS", dst: "Onboarding" }
      must_have_valid_paths: true
      groundedness_min: 0.8
    min_score: 0.7
  - question: "How many employees joined in 2025?"
    mode: sql
    expects:
      answer_regex: "\\\\d+"
    min_score: 0.5
"""


def _valid_golden() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_VALID_GOLDEN_YAML))


def test_valid_golden_set_passes(golden_validator: jsonschema.Draft202012Validator) -> None:
    """A §20/§27.5-shaped golden set (every expects field exercised) must
    validate — otherwise the schema is stricter than the design and rejects
    golden sets the spec calls legitimate."""
    golden_validator.validate(_valid_golden())


def _missing_question(g: dict[str, Any]) -> None:
    del g["cases"][0]["question"]


def _empty_question(g: dict[str, Any]) -> None:
    g["cases"][0]["question"] = ""


def _missing_mode(g: dict[str, Any]) -> None:
    del g["cases"][0]["mode"]


def _unknown_mode(g: dict[str, Any]) -> None:
    g["cases"][0]["mode"] = "vector"


def _missing_expects(g: dict[str, Any]) -> None:
    del g["cases"][0]["expects"]


def _empty_expects(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"] = {}


def _typoed_expects_field(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_contain_entites"] = ["People Ops"]


def _unknown_case_field(g: dict[str, Any]) -> None:
    g["cases"][0]["notes"] = "flaky"


def _unknown_top_level_field(g: dict[str, Any]) -> None:
    g["golden"] = True


def _missing_min_score(g: dict[str, Any]) -> None:
    del g["cases"][0]["min_score"]


def _min_score_above_one(g: dict[str, Any]) -> None:
    g["cases"][0]["min_score"] = 1.5


def _min_score_negative(g: dict[str, Any]) -> None:
    g["cases"][0]["min_score"] = -0.1


def _groundedness_min_above_one(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["groundedness_min"] = 1.2


def _empty_entities_list(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_contain_entities"] = []


def _blank_entity_name(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_contain_entities"] = [""]


def _empty_sources_list(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_cite_sources"] = []


def _relation_missing_dst(g: dict[str, Any]) -> None:
    del g["cases"][0]["expects"]["must_include_relations"][0]["dst"]


def _relation_blank_type(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_include_relations"][0]["type"] = ""


def _relation_extra_field(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_include_relations"][0]["hops"] = 2


def _valid_paths_non_boolean(g: dict[str, Any]) -> None:
    g["cases"][0]["expects"]["must_have_valid_paths"] = "yes"


def _broken_answer_regex(g: dict[str, Any]) -> None:
    g["cases"][1]["expects"]["answer_regex"] = "["


def _wrong_schema_version(g: dict[str, Any]) -> None:
    g["schema_version"] = "2.0"


def _missing_schema_version(g: dict[str, Any]) -> None:
    del g["schema_version"]


def _empty_cases(g: dict[str, Any]) -> None:
    g["cases"] = []


def _missing_cases(g: dict[str, Any]) -> None:
    del g["cases"]


@pytest.mark.parametrize(
    "mutate",
    [
        _missing_question,
        _empty_question,
        _missing_mode,
        _unknown_mode,
        _missing_expects,
        _empty_expects,
        _typoed_expects_field,
        _unknown_case_field,
        _unknown_top_level_field,
        _missing_min_score,
        _min_score_above_one,
        _min_score_negative,
        _groundedness_min_above_one,
        _empty_entities_list,
        _blank_entity_name,
        _empty_sources_list,
        _relation_missing_dst,
        _relation_blank_type,
        _relation_extra_field,
        _valid_paths_non_boolean,
        _broken_answer_regex,
        _wrong_schema_version,
        _missing_schema_version,
        _empty_cases,
        _missing_cases,
    ],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_golden_schema_rejects_contract_violations(
    golden_validator: jsonschema.Draft202012Validator,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """The false-green hazards must *bite*: an empty golden set, an expectation
    that can never run (typo, empty list, broken regex) or an out-of-vocabulary
    mode/version is rejected at authoring time, not silently skipped at eval
    time."""
    golden = copy.deepcopy(_valid_golden())
    mutate(golden)
    with pytest.raises(jsonschema.ValidationError):
        golden_validator.validate(golden)
