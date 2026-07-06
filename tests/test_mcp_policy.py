"""Why: the query policy is the guardrail's configuration — a misloaded or
half-validated policy runs the server under-guarded. What must hold: the
frozen contract validates BEFORE any value is trusted (violations fail loud
at startup, naming where), the §21 typed re-check still bites after schema
validation, and ceiling reconciliation follows the C6b caller contract
(mode functions only ever see reconciled values).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from core.mcp.policy import PolicyError, QueryPolicy, load_query_policy


def _valid_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "default_mode": "hybrid",
        "max_top_k": 20,
        "max_graph_hops": 3,
        "max_sql_rows": 100,
        "max_latency_ms": 10000,
        "require_sources": True,
        "expose_debug": True,
        "text_to_sql": {
            "enabled": True,
            "readonly": True,
            "allowed_tables": ["orders"],
            "blocked_keywords": ["insert", "update", "delete", "drop", "alter", "truncate"],
            "max_rows": 60,
            "timeout_ms": 5000,
        },
        "text_to_cypher": {
            "enabled": False,
            "readonly": True,
            "allowed_clauses": ["MATCH", "WHERE", "RETURN", "LIMIT"],
            "blocked": ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
            "max_rows": 100,
            "timeout_ms": 5000,
        },
    }


def _write(tmp_path: Path, document: dict[str, Any] | str) -> Path:
    path = tmp_path / "config.yaml"
    if isinstance(document, str):
        path.write_text(document, "utf-8")
    else:
        path.write_text(yaml.safe_dump({"query_policy": document}), "utf-8")
    return path


def test_a_valid_policy_loads_typed_and_reconciled(tmp_path: Path) -> None:
    policy = load_query_policy(_write(tmp_path, _valid_document()))
    assert isinstance(policy, QueryPolicy)
    assert policy.default_mode == "hybrid" and policy.expose_debug is True
    assert policy.text_to_sql.allowed_tables == ("orders",)
    # C6b caller-reconciliation: the min of top-level and mode-local caps
    assert policy.sql_rows() == 60
    # top_k: no ask → the cap; over-ask → clamped; under-ask → honored
    assert policy.top_k(None) == 20
    assert policy.top_k(50) == 20
    assert policy.top_k(5) == 5


def test_a_missing_config_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_query_policy(tmp_path / "nope.yaml")


def test_invalid_yaml_and_missing_block_fail_loud(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_query_policy(_write(tmp_path, ":\n  - ]["))
    with pytest.raises(PolicyError, match="no query_policy block"):
        load_query_policy(_write(tmp_path, "other: {}"))


def test_a_contract_violation_names_where_it_broke(tmp_path: Path) -> None:
    """DR-002: the frozen schema is the gate — a violating document must fail
    BEFORE any value is trusted, pointing at the offending path."""
    document = _valid_document()
    document["max_top_k"] = 0  # schema minimum is 1
    with pytest.raises(PolicyError, match="max_top_k"):
        load_query_policy(_write(tmp_path, document))

    document = _valid_document()
    del document["expose_debug"]  # required
    with pytest.raises(PolicyError, match="expose_debug"):
        load_query_policy(_write(tmp_path, document))


def test_the_schema_is_the_first_gate_for_the_deny_all_contradiction(
    tmp_path: Path,
) -> None:
    """An enabled sql mode with an empty whitelist is rejected by the FROZEN
    SCHEMA itself (minItems — the first gate), so the loader's typed §21
    re-check behind it is pure defense in depth: unreachable through this
    loader for contract-valid documents, and covered on its own in
    test_query_policy_model.py for values that arrive by other paths."""
    document = _valid_document()
    document["text_to_sql"]["allowed_tables"] = []  # enabled + empty
    with pytest.raises(PolicyError, match="allowed_tables"):
        load_query_policy(_write(tmp_path, document))


def test_the_top_level_latency_cap_governs_mode_deadlines(tmp_path: Path) -> None:
    """§21: max_latency_ms is THE query deadline — a mode-local timeout above
    it would let one DB phase alone outlive the whole query's budget. C8 loads
    both values, so C8 reconciles (the same min() contract as the row caps);
    a mode timeout BELOW the cap is left alone."""
    document = _valid_document()
    document["max_latency_ms"] = 2000  # below both mode timeouts (5000)
    policy = load_query_policy(_write(tmp_path, document))
    assert policy.sql_policy().timeout_ms == 2000
    assert policy.cypher_policy().timeout_ms == 2000
    # everything else rides along unchanged
    assert policy.sql_policy().allowed_tables == policy.text_to_sql.allowed_tables

    document = _valid_document()
    document["max_latency_ms"] = 60000  # above the mode timeouts
    policy = load_query_policy(_write(tmp_path, document))
    assert policy.sql_policy().timeout_ms == 5000  # the smaller mode value holds
    assert policy.cypher_policy().timeout_ms == 5000


def test_the_schema_resolves_from_candidates_and_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source checkout reads contracts/ at the repo root; an installed wheel
    reads the build-time copy shipped inside core/ (force-include). Neither
    present → PolicyError naming every candidate, at startup — never a bare
    FileNotFoundError mid-config-load."""
    import core.mcp.policy as module

    real_schema = module._SCHEMA_CANDIDATES[0].read_text("utf-8")
    packaged = tmp_path / "core" / "contracts" / "query_policy.schema.json"
    packaged.parent.mkdir(parents=True)
    packaged.write_text(real_schema, "utf-8")
    # repo-root candidate missing → the packaged copy is used
    monkeypatch.setattr(module, "_SCHEMA_CANDIDATES", (tmp_path / "nope.json", packaged))
    policy = load_query_policy(_write(tmp_path, _valid_document()))
    assert policy.max_top_k == 20  # loaded through the fallback candidate

    monkeypatch.setattr(
        module, "_SCHEMA_CANDIDATES", (tmp_path / "nope.json", tmp_path / "also-nope.json")
    )
    with pytest.raises(PolicyError, match="not found"):
        load_query_policy(_write(tmp_path, _valid_document()))
