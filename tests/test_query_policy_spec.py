"""Why: the C6b/C6c guardrail validators will enforce exactly these
vocabularies — if the frozen lists drift from §21/§27.6 (or contradict each
other), the enforcement layer and the config contract disagree about what the
guardrail *is*, and a query one layer blocks the other would have executed.
"""

from __future__ import annotations

from core.query.policy import (
    CYPHER_ALLOWED_CLAUSES,
    CYPHER_BLOCKED_MIN,
    GRAPH_QUERY_TEMPLATES,
    GUARDRAIL_WARNING_CODE,
    SQL_BLOCKED_KEYWORDS_MIN,
)


def test_sql_blocked_minimum_freezes_design() -> None:
    assert SQL_BLOCKED_KEYWORDS_MIN == ("insert", "update", "delete", "drop", "alter", "truncate")


def test_cypher_vocabularies_freeze_design() -> None:
    assert CYPHER_ALLOWED_CLAUSES == ("MATCH", "WHERE", "RETURN", "LIMIT")
    assert CYPHER_BLOCKED_MIN == ("CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL")


def test_allowed_and_blocked_clauses_are_disjoint() -> None:
    """A clause both whitelisted and blocked would make the guardrail's answer
    depend on evaluation order — the two frozen sets must never overlap."""
    assert not set(CYPHER_ALLOWED_CLAUSES) & set(CYPHER_BLOCKED_MIN)


def test_graph_templates_freeze_design() -> None:
    """§27.6: the parameterized templates are the default graph path; MCP
    (C8) and the graph retriever (C6c) both key off these names."""
    assert GRAPH_QUERY_TEMPLATES == ("neighbors", "path", "subgraph")


def test_vocabularies_have_no_duplicates() -> None:
    for vocab in (
        SQL_BLOCKED_KEYWORDS_MIN,
        CYPHER_ALLOWED_CLAUSES,
        CYPHER_BLOCKED_MIN,
        GRAPH_QUERY_TEMPLATES,
    ):
        assert len(vocab) == len(set(vocab))


def test_guardrail_warning_code_value() -> None:
    """The exact string consumers see in warnings[].code (§21 typed rejection);
    membership in the frozen §27.2 enums is pinned by the contract test."""
    assert GUARDRAIL_WARNING_CODE == "GUARDRAIL_BLOCKED"
