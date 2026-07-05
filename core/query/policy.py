"""Query guardrail strategy vocabulary (DESIGN §21/§27.6, Track 0 P5).

Freezes the *strategy* the guardrail implementations (C6b sql, C6c graph) are
built against — the executable validators land with those tasks, but the
vocabularies they enforce are contract and live here, mirrored by
``contracts/query_policy.schema.json`` (contract tests assert the two never
drift):

- **SQL** (§27.6): parse with sqlglot into an AST *before* execution — string
  matching alone is not validation. Reject on: parse failure, more than one
  statement, any table outside ``allowed_tables``, any DDL/DML. The blocked
  keyword list is defense in depth on top of the AST check;
  ``SQL_BLOCKED_KEYWORDS_MIN`` is §21's frozen minimum (extend, never shrink).
- **Cypher** (§27.6): MCP graph tools default to parameterized query
  templates (``GRAPH_QUERY_TEMPLATES``); free NL→Cypher is optional and, when
  enabled, restricted by a Cypher parser to ``CYPHER_ALLOWED_CLAUSES`` —
  nothing outside that universe can ever be whitelisted. ``CYPHER_BLOCKED_MIN``
  is §21's frozen minimum; blocking ``CALL`` bans every procedure, APOC
  included.

Violations are rejected with the typed ``GUARDRAIL_BLOCKED`` warning (§21) —
``GUARDRAIL_WARNING_CODE`` is pinned to the frozen §27.2 warning enum by a
contract test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: §21 frozen minimum for text_to_sql.blocked_keywords (lowercase canonical;
#: matching is case-insensitive). Projects extend, never shrink.
SQL_BLOCKED_KEYWORDS_MIN = ("insert", "update", "delete", "drop", "alter", "truncate")

#: §21 frozen clause universe for text_to_cypher.allowed_clauses — the widest
#: whitelist a project may configure.
CYPHER_ALLOWED_CLAUSES = ("MATCH", "WHERE", "RETURN", "LIMIT")

#: §21 frozen minimum for text_to_cypher.blocked (uppercase canonical).
#: CALL covers all procedures, APOC included. Precedence on overlap: a
#: project-extended blocked list is authoritative over allowed_clauses — the
#: C6c validator rejects a clause that appears in both (blocklist wins, the
#: guardrail may only over-block, never under-block).
CYPHER_BLOCKED_MIN = ("CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL")

#: §27.6: the parameterized graph-tool templates that are the *default* graph
#: path — free NL→Cypher never replaces these, it is an optional extra.
GRAPH_QUERY_TEMPLATES = ("neighbors", "path", "subgraph")

#: §21: policy violations come back as this typed warning code (frozen in the
#: §27.2 warning enum), never as an executed query or an untyped error.
GUARDRAIL_WARNING_CODE = "GUARDRAIL_BLOCKED"


@dataclass(frozen=True)
class TextToSql:
    """Typed mirror of ``query_policy.text_to_sql`` (§21/§27.6), the config the
    C6b guardrail + executor consume.

    ``contracts/query_policy.schema.json`` already validates a policy document
    structurally; this is the in-code value the executing seam holds, and it
    RE-CHECKS at construction the three frozen §21 guarantees the guardrail
    actually relies on — so a policy that somehow reached here malformed fails
    loud instead of silently under-guarding (the value-validation discipline of
    :mod:`core.query.results`):

    - ``readonly`` is frozen ``True`` — a writable NL→SQL path is forbidden and
      unrepresentable (schema ``const: true``);
    - ``blocked_keywords`` covers :data:`SQL_BLOCKED_KEYWORDS_MIN` — a project
      may extend the list, never shrink it below the frozen six;
    - an ``enabled`` mode has a non-empty ``allowed_tables`` — an enabled empty
      whitelist is a deny-all contradiction (use ``enabled=False`` for that);
    - ``max_rows`` and ``timeout_ms`` are ``≥ 1`` (the schema minimum) — a
      non-positive ``timeout_ms`` would render ``statement_timeout = 0``, which
      *disables* the Postgres deadline the executor relies on (§21/§22).

    The row/latency reconciliation with the top-level ``max_sql_rows`` /
    ``max_latency_ms`` is the caller's job (C6b takes an already-reconciled
    ``max_rows``); this block carries the sql-mode-local caps.
    """

    enabled: bool
    allowed_tables: tuple[str, ...]
    blocked_keywords: tuple[str, ...]
    max_rows: int
    timeout_ms: int
    readonly: bool = True

    def __post_init__(self) -> None:
        if self.readonly is not True:
            raise ValueError(
                "text_to_sql.readonly is frozen true (§21) — a writable NL→SQL path is forbidden"
            )
        present = {word.lower() for word in self.blocked_keywords}
        missing = [word for word in SQL_BLOCKED_KEYWORDS_MIN if word not in present]
        if missing:
            raise ValueError(
                f"blocked_keywords is missing the frozen §21 minimum {missing} — "
                "the list may be extended, never shrunk below the frozen six"
            )
        if self.enabled and not self.allowed_tables:
            raise ValueError(
                "an enabled text_to_sql needs a non-empty allowed_tables — an enabled "
                "empty whitelist is a deny-all contradiction (use enabled=False to deny all)"
            )
        if self.max_rows < 1 or self.timeout_ms < 1:
            raise ValueError(
                f"max_rows ({self.max_rows}) and timeout_ms ({self.timeout_ms}) are frozen "
                "≥ 1 (§21 schema minimum) — a non-positive timeout_ms disables the deadline"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TextToSql:
        """Build from a (schema-validated) ``text_to_sql`` mapping."""
        return cls(
            enabled=bool(data["enabled"]),
            allowed_tables=tuple(data["allowed_tables"]),
            blocked_keywords=tuple(data["blocked_keywords"]),
            max_rows=int(data["max_rows"]),
            timeout_ms=int(data["timeout_ms"]),
            readonly=bool(data.get("readonly", True)),
        )
