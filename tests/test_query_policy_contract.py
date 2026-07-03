"""Contract tests — contracts/query_policy.schema.json (Track 0 P5, DESIGN §21/§27.6).

query_policy is the guardrail every query/MCP consumer enforces: if the schema
under-rejects, a typo'd limit or a shrunken blocklist silently weakens the
safety net (writable SQL, APOC whitelisted); if it over-rejects, legitimate
policies can't be written. These tests pin both directions, the enum lockstep
with the other frozen contracts, and the schema↔core-vocabulary lockstep.
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

from core.query.policy import (
    CYPHER_ALLOWED_CLAUSES,
    CYPHER_BLOCKED_MIN,
    GUARDRAIL_WARNING_CODE,
    SQL_BLOCKED_KEYWORDS_MIN,
)

pytestmark = pytest.mark.contract

_CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
_POLICY_SCHEMA = _CONTRACTS / "query_policy.schema.json"
_MCP_SCHEMA = _CONTRACTS / "mcp_response.schema.json"
_GOLDEN_SCHEMA = _CONTRACTS / "golden.schema.json"
_OPENAPI = _CONTRACTS / "openapi.yaml"


@pytest.fixture(scope="module")
def policy_schema() -> dict[str, Any]:
    assert _POLICY_SCHEMA.exists(), (
        "contracts/query_policy.schema.json is the frozen Track 0 P5 deliverable"
    )
    return cast(dict[str, Any], json.loads(_POLICY_SCHEMA.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def policy_validator(policy_schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        policy_schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def test_policy_schema_is_valid(policy_schema: dict[str, Any]) -> None:
    """The frozen deliverable must be a valid Draft 2020-12 schema."""
    jsonschema.Draft202012Validator.check_schema(policy_schema)


def test_policy_schema_version_is_frozen(policy_schema: dict[str, Any]) -> None:
    """DR-002: schema_version pins the contract; only a breaking change bumps it."""
    assert policy_schema["properties"]["schema_version"]["const"] == "1.0"
    assert "schema_version" in policy_schema["required"]


def test_query_mode_stays_in_lockstep_with_other_contracts(policy_schema: dict[str, Any]) -> None:
    """default_mode must name one of the five frozen retrieval modes — drift
    would let a policy default to a mode the query surface doesn't have."""
    policy_modes = set(policy_schema["$defs"]["QueryMode"]["enum"])
    mcp = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    golden = json.loads(_GOLDEN_SCHEMA.read_text(encoding="utf-8"))
    api = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    assert policy_modes == set(mcp["$defs"]["QueryMode"]["enum"])
    assert policy_modes == set(golden["$defs"]["QueryMode"]["enum"])
    assert policy_modes == set(api["components"]["schemas"]["QueryMode"]["enum"])


def _contains_consts(field_schema: dict[str, Any]) -> set[str]:
    """Extract the frozen-minimum set encoded as allOf/contains const clauses."""
    return {clause["contains"]["const"] for clause in field_schema["allOf"]}


def test_guardrail_vocabulary_matches_core_spec(policy_schema: dict[str, Any]) -> None:
    """core.query.policy holds the vocabulary the C6b/C6c validators enforce;
    the schema holds what projects may configure. If the two drift, the config
    layer and the enforcement layer disagree about the guardrail itself."""
    sql = policy_schema["$defs"]["TextToSql"]["properties"]
    cypher = policy_schema["$defs"]["TextToCypher"]["properties"]
    assert _contains_consts(sql["blocked_keywords"]) == set(SQL_BLOCKED_KEYWORDS_MIN)
    assert set(cypher["allowed_clauses"]["items"]["enum"]) == set(CYPHER_ALLOWED_CLAUSES)
    assert _contains_consts(cypher["blocked"]) == set(CYPHER_BLOCKED_MIN)


def test_guardrail_warning_code_is_in_frozen_enums() -> None:
    """§21: violations come back as a typed warning — the code the policy layer
    emits must exist in both frozen warning enums or consumers can't type it."""
    mcp = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    api = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    assert GUARDRAIL_WARNING_CODE in mcp["$defs"]["WarningCode"]["enum"]
    assert GUARDRAIL_WARNING_CODE in api["components"]["schemas"]["WarningCode"]["enum"]


# Authored as YAML because the real artifact is a config.yaml block (§12/§21).
_VALID_POLICY_YAML = """
schema_version: "1.0"
default_mode: hybrid
max_top_k: 20
max_graph_hops: 3
max_sql_rows: 200
max_latency_ms: 8000
require_sources: true
expose_debug: false
text_to_sql:
  enabled: true
  readonly: true
  allowed_tables: [employees, teams]
  blocked_keywords: [insert, update, delete, drop, alter, truncate]
  max_rows: 200
  timeout_ms: 4000
text_to_cypher:
  enabled: false
  readonly: true
  allowed_clauses: [MATCH, WHERE, RETURN, LIMIT]
  blocked: [CREATE, MERGE, DELETE, SET, REMOVE, CALL]
  max_rows: 500
  timeout_ms: 4000
"""


def _valid_policy() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_VALID_POLICY_YAML))


def test_valid_policy_passes(policy_validator: jsonschema.Draft202012Validator) -> None:
    """A §21-shaped policy must validate — otherwise the schema is stricter
    than the design and rejects policies the spec calls legitimate."""
    policy_validator.validate(_valid_policy())


def test_disabled_sql_may_have_empty_whitelist(
    policy_validator: jsonschema.Draft202012Validator,
) -> None:
    """enabled: false with an empty allowed_tables is coherent (mode off, no
    whitelist to maintain) — requiring dummy table names there would force
    lies into the config."""
    policy = _valid_policy()
    policy["text_to_sql"]["enabled"] = False
    policy["text_to_sql"]["allowed_tables"] = []
    policy_validator.validate(policy)


def test_sql_default_mode_requires_sql_enabled(
    policy_validator: jsonschema.Draft202012Validator,
) -> None:
    """default_mode: sql is legal exactly when the sql mode can actually run —
    with it enabled the same policy validates; with it disabled every default
    query would be rejected by this very contract (see the reject mutation)."""
    policy = _valid_policy()
    policy["default_mode"] = "sql"
    policy_validator.validate(policy)


def test_graph_default_with_cypher_disabled_passes(
    policy_validator: jsonschema.Draft202012Validator,
) -> None:
    """Deliberately NOT the sql contradiction: graph's default path is
    parameterized templates (§27.6) — text_to_cypher.enabled only gates the
    optional free NL→Cypher add-on, so graph as default_mode must stay legal
    while that flag is false, or the template path becomes unconfigurable."""
    policy = _valid_policy()
    policy["default_mode"] = "graph"
    assert policy["text_to_cypher"]["enabled"] is False
    policy_validator.validate(policy)


def test_extended_blocklists_pass(policy_validator: jsonschema.Draft202012Validator) -> None:
    """§21 lists are frozen *minimums* — projects may extend them (additive),
    only shrinking is illegal."""
    policy = _valid_policy()
    policy["text_to_sql"]["blocked_keywords"].append("grant")
    policy["text_to_cypher"]["blocked"].append("LOAD_CSV")
    policy_validator.validate(policy)


def _typoed_top_level_field(p: dict[str, Any]) -> None:
    p["max_topk"] = 20


def _missing_default_mode(p: dict[str, Any]) -> None:
    del p["default_mode"]


def _unknown_default_mode(p: dict[str, Any]) -> None:
    p["default_mode"] = "vector"


def _missing_text_to_sql(p: dict[str, Any]) -> None:
    del p["text_to_sql"]


def _zero_top_k(p: dict[str, Any]) -> None:
    p["max_top_k"] = 0


def _zero_latency(p: dict[str, Any]) -> None:
    p["max_latency_ms"] = 0


def _negative_graph_hops(p: dict[str, Any]) -> None:
    p["max_graph_hops"] = -1


def _require_sources_false(p: dict[str, Any]) -> None:
    """§16/§27.2 structurally mandate source_refs — a policy promising
    sourceless answers would contradict the frozen response contracts."""
    p["require_sources"] = False


def _sql_readonly_false(p: dict[str, Any]) -> None:
    """§21/§27.6: writable NL→SQL is unrepresentable."""
    p["text_to_sql"]["readonly"] = False


def _cypher_readonly_false(p: dict[str, Any]) -> None:
    p["text_to_cypher"]["readonly"] = False


def _enabled_sql_with_empty_whitelist(p: dict[str, Any]) -> None:
    """enabled + empty whitelist is a contradiction, not deny-all."""
    p["text_to_sql"]["allowed_tables"] = []


def _sql_default_mode_while_disabled(p: dict[str, Any]) -> None:
    """Defaulting to a mode this same policy disables would make every
    default query dead on arrival — same contradiction class as
    enabled_sql_with_empty_whitelist, caught at authoring time."""
    p["default_mode"] = "sql"
    p["text_to_sql"]["enabled"] = False
    p["text_to_sql"]["allowed_tables"] = []


def _blank_table_name(p: dict[str, Any]) -> None:
    p["text_to_sql"]["allowed_tables"] = ["employees", ""]


def _duplicate_table(p: dict[str, Any]) -> None:
    p["text_to_sql"]["allowed_tables"] = ["employees", "employees"]


def _shrunken_sql_blocklist(p: dict[str, Any]) -> None:
    """Removing a frozen-core keyword (drop) re-opens DDL — the §21 minimum
    must be non-negotiable."""
    p["text_to_sql"]["blocked_keywords"] = ["insert", "update", "delete", "alter", "truncate"]


def _uppercase_sql_keyword(p: dict[str, Any]) -> None:
    """lowercase is canonical; 'DROP' would evade a naive exact-match consumer."""
    p["text_to_sql"]["blocked_keywords"] = [
        "insert",
        "update",
        "delete",
        "DROP",
        "alter",
        "truncate",
    ]


def _sql_field_typo(p: dict[str, Any]) -> None:
    p["text_to_sql"]["allowed_table"] = ["employees"]


def _zero_sql_rows(p: dict[str, Any]) -> None:
    p["text_to_sql"]["max_rows"] = 0


def _call_whitelisted(p: dict[str, Any]) -> None:
    """CALL (and thus APOC) must be impossible to whitelist (§21/§27.6)."""
    p["text_to_cypher"]["allowed_clauses"] = ["MATCH", "RETURN", "CALL"]


def _empty_allowed_clauses(p: dict[str, Any]) -> None:
    p["text_to_cypher"]["allowed_clauses"] = []


def _shrunken_cypher_blocklist(p: dict[str, Any]) -> None:
    p["text_to_cypher"]["blocked"] = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE"]


def _lowercase_cypher_blocked(p: dict[str, Any]) -> None:
    p["text_to_cypher"]["blocked"] = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "call"]


def _zero_cypher_timeout(p: dict[str, Any]) -> None:
    p["text_to_cypher"]["timeout_ms"] = 0


def _wrong_schema_version(p: dict[str, Any]) -> None:
    p["schema_version"] = "2.0"


def _missing_schema_version(p: dict[str, Any]) -> None:
    del p["schema_version"]


@pytest.mark.parametrize(
    "mutate",
    [
        _typoed_top_level_field,
        _missing_default_mode,
        _unknown_default_mode,
        _missing_text_to_sql,
        _zero_top_k,
        _zero_latency,
        _negative_graph_hops,
        _require_sources_false,
        _sql_readonly_false,
        _cypher_readonly_false,
        _enabled_sql_with_empty_whitelist,
        _sql_default_mode_while_disabled,
        _blank_table_name,
        _duplicate_table,
        _shrunken_sql_blocklist,
        _uppercase_sql_keyword,
        _sql_field_typo,
        _zero_sql_rows,
        _call_whitelisted,
        _empty_allowed_clauses,
        _shrunken_cypher_blocklist,
        _lowercase_cypher_blocked,
        _zero_cypher_timeout,
        _wrong_schema_version,
        _missing_schema_version,
    ],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_policy_schema_rejects_contract_violations(
    policy_validator: jsonschema.Draft202012Validator,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """The guardrail must be un-weakenable at config time: shrunken blocklists,
    whitelisted CALL, writable modes, no-op limits and typo'd fields are
    rejected at authoring time — not discovered when a query slips through."""
    policy = copy.deepcopy(_valid_policy())
    mutate(policy)
    with pytest.raises(jsonschema.ValidationError):
        policy_validator.validate(policy)
