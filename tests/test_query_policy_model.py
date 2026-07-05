"""Why: TextToSql is the in-code value the SQL guardrail + executor consume, and
it RE-CHECKS at construction the three frozen §21 guarantees they rely on. If a
malformed policy could construct silently, the guardrail would run with a
shrunken blocked list, a writable role, or a deny-all-that-reads-as-enabled —
each a real weakening. These tests pin that the model fails loud instead.
"""

from __future__ import annotations

import pytest

from core.query.policy import (
    CYPHER_BLOCKED_MIN,
    SQL_BLOCKED_KEYWORDS_MIN,
    TextToCypher,
    TextToSql,
)

_VALID = {
    "enabled": True,
    "readonly": True,
    "allowed_tables": ["orders", "customers"],
    "blocked_keywords": list(SQL_BLOCKED_KEYWORDS_MIN),
    "max_rows": 100,
    "timeout_ms": 5000,
}


def test_from_mapping_round_trips_a_valid_block() -> None:
    """A schema-valid text_to_sql mapping builds the typed value with its fields
    intact — the shape the executor reads."""
    sql = TextToSql.from_mapping(_VALID)
    assert sql.enabled is True
    assert sql.allowed_tables == ("orders", "customers")
    assert set(SQL_BLOCKED_KEYWORDS_MIN) <= set(sql.blocked_keywords)
    assert sql.max_rows == 100 and sql.timeout_ms == 5000


def test_readonly_false_is_rejected() -> None:
    """§21 freezes readonly true — a writable NL→SQL path is unrepresentable, so
    a false slips through neither the schema nor the model."""
    with pytest.raises(ValueError, match="readonly"):
        TextToSql.from_mapping({**_VALID, "readonly": False})


def test_blocked_keywords_below_the_frozen_minimum_is_rejected() -> None:
    """The frozen six [insert…truncate] are a floor: a project may extend the
    list, never shrink it. A list missing one is refused so the keyword defense
    can't be silently disabled."""
    short = [word for word in SQL_BLOCKED_KEYWORDS_MIN if word != "delete"]
    with pytest.raises(ValueError, match="minimum"):
        TextToSql.from_mapping({**_VALID, "blocked_keywords": short})


def test_enabled_with_an_empty_whitelist_is_rejected() -> None:
    """An enabled mode with no allowed_tables is a deny-all contradiction — the
    whitelist is what the guardrail enforces against, so an empty one while
    enabled is a config bug, not a way to deny all (use enabled=False)."""
    with pytest.raises(ValueError, match="allowed_tables"):
        TextToSql.from_mapping({**_VALID, "allowed_tables": []})


def test_disabled_may_have_an_empty_whitelist() -> None:
    """A disabled mode legitimately carries an empty whitelist — nothing runs, so
    there is nothing to whitelist."""
    sql = TextToSql.from_mapping({**_VALID, "enabled": False, "allowed_tables": []})
    assert sql.enabled is False and sql.allowed_tables == ()


@pytest.mark.parametrize("field", ["max_rows", "timeout_ms"])
def test_non_positive_caps_are_rejected(field: str) -> None:
    """The schema freezes both caps at ≥ 1; the model re-checks it because a
    timeout_ms of 0 would render `statement_timeout = 0` — Postgres's value for
    NO deadline — silently disabling the very guardrail §21/§22 relies on."""
    with pytest.raises(ValueError, match="timeout_ms"):
        TextToSql.from_mapping({**_VALID, field: 0})


# -- TextToCypher (C6c): the same fail-loud discipline for the graph block ------

_VALID_CYPHER = {
    "enabled": False,
    "readonly": True,
    "allowed_clauses": ["MATCH", "WHERE", "RETURN", "LIMIT"],
    "blocked": ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
    "max_rows": 50,
    "timeout_ms": 3000,
}


def test_cypher_from_mapping_round_trips_a_valid_block() -> None:
    cypher = TextToCypher.from_mapping(_VALID_CYPHER)
    assert cypher.enabled is False
    assert cypher.allowed_clauses == ("MATCH", "WHERE", "RETURN", "LIMIT")
    assert set(CYPHER_BLOCKED_MIN) <= set(cypher.blocked)
    assert cypher.max_rows == 50 and cypher.timeout_ms == 3000


def test_cypher_readonly_false_is_rejected() -> None:
    with pytest.raises(ValueError, match="readonly"):
        TextToCypher.from_mapping({**_VALID_CYPHER, "readonly": False})


def test_a_clause_outside_the_frozen_universe_is_rejected() -> None:
    """§21 freezes the widest whitelist at MATCH/WHERE/RETURN/LIMIT — CALL (and
    with it every procedure, APOC included) can never be whitelisted in, so a
    policy that tries is a config bug refused at construction."""
    with pytest.raises(ValueError, match="frozen §21 universe"):
        TextToCypher.from_mapping({**_VALID_CYPHER, "allowed_clauses": ["MATCH", "CALL"]})
    with pytest.raises(ValueError, match="non-empty"):
        TextToCypher.from_mapping({**_VALID_CYPHER, "allowed_clauses": []})


def test_cypher_blocked_below_the_frozen_minimum_is_rejected() -> None:
    short = [word for word in CYPHER_BLOCKED_MIN if word != "CALL"]
    with pytest.raises(ValueError, match="minimum"):
        TextToCypher.from_mapping({**_VALID_CYPHER, "blocked": short})


@pytest.mark.parametrize("field", ["max_rows", "timeout_ms"])
def test_cypher_non_positive_caps_are_rejected(field: str) -> None:
    """The graph mode's deadline rides the Neo4j Query timeout — a non-positive
    value would disable the §21/§22 ceiling, so the model refuses it."""
    with pytest.raises(ValueError, match="timeout_ms"):
        TextToCypher.from_mapping({**_VALID_CYPHER, field: 0})
