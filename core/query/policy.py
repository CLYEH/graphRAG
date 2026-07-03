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
