"""SQL guardrail: sqlglot AST validation before execution (DESIGN §21/§27.6, C6b).

§27.6 freezes the strategy: parse the candidate SQL with ``sqlglot`` into an AST
*before* execution — string matching alone is not validation — and reject
anything that is not a single, read-only, whitelisted-table query. This module
is the executable form of that strategy; :mod:`core.query.policy` holds the
vocabulary it enforces (``SQL_BLOCKED_KEYWORDS_MIN``). A rejection is surfaced as
the typed ``GUARDRAIL_BLOCKED`` warning (§21), never executed.

**v1 shape — a FLAT SINGLE-TABLE ``SELECT *``**::

    SELECT * FROM <one whitelisted table> [WHERE ...] [ORDER BY ...] [LIMIT ...]

Everything else is rejected. The restriction is not arbitrary tightness — it is
what makes the result CITABLE. §16/§27.2 ``require_sources`` demands every result
row cite exactly one source row by ``(table, pk)``; an aggregate, a ``GROUP BY``,
a join or a set operation produces rows that fold many source rows or none, which
cannot carry a single ``(table, pk)``. A flat ``SELECT *`` returns whole source
rows, each citable by its pk. Analytics/joins are a future extension with a
richer citation model — not a v1 path that silently emits uncitable rows.

The whitelist is enforced on the *bare* table name only; a schema-qualified name
(``public.orders``, ``pg_catalog.…``) is rejected, because the executor
reconstructs each logical table as a build-scoped CTE of the *same bare name*
(:mod:`core.stores.sqlreader`) — a qualified reference would slip past that CTE
to a real base table. Keyword blocking is defense in depth on top of the AST and
runs on the token stream, so a blocked word inside a string literal or a quoted
identifier (``WHERE note = 'please delete this'``) does not false-trip it.

Read-only is not just "no DML root": a ``SELECT`` can still mutate session or
transaction state through a FUNCTION (``pg_advisory_lock``, ``set_config``,
``nextval``) — leaving a pooled connection carrying locks/settings — or through
``FOR UPDATE`` (row locks) or ``SELECT INTO`` (writes a table), none of which the
deferred read-only role would stop. So within the flat ``SELECT *`` only a type
CAST is permitted (``amount::numeric``); every other function call, any row-lock
clause, and ``SELECT INTO`` are refused. This is the allow-list stance §21 asks
for — the guardrail may over-block, never under-block.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

import sqlglot
from sqlglot import TokenType, exp

_DIALECT = "postgres"

#: AST node types that make a row uncitable (fold/duplicate/cross source rows)
#: or step outside a flat single-table read — each rejected wholesale (v1).
_FORBIDDEN: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.With, "a WITH / CTE clause"),
    (exp.Join, "a JOIN"),
    (exp.Subquery, "a subquery"),
    (exp.Union, "a UNION / EXCEPT / INTERSECT"),
    (exp.Group, "a GROUP BY"),
    (exp.AggFunc, "an aggregate function"),
    (exp.Distinct, "DISTINCT"),
    # TABLESAMPLE returns an arbitrary subset BEFORE the WHERE, so the result would
    # silently omit matching rows — outside the frozen v1 shape (WHERE/ORDER BY/LIMIT).
    (exp.TableSample, "TABLESAMPLE"),
    # OFFSET skips leading matching rows — the answer would look complete while
    # silently dropping the first N citable rows; not part of the v1 shape.
    (exp.Offset, "an OFFSET clause"),
    # bind placeholders (:x / ? / @x) are never legitimate in a literal NL→SQL
    # result, and the executor injects its OWN scope placeholders around this
    # query — a user placeholder would collide with that binding.
    (exp.Placeholder, "a bind placeholder"),
    (exp.Parameter, "a bind parameter"),
)

#: Token types that are NOT keywords — a blocked word appearing as one of these
#: (a string literal, or a deliberately quoted identifier) is legitimate data,
#: not a DML verb, so the keyword scan skips them (no over-block, Rule 9 dual).
_NON_KEYWORD_TOKENS = frozenset({TokenType.STRING, TokenType.IDENTIFIER})


class GuardrailBlocked(Exception):
    """A candidate SQL string violated the §21/§27.6 guardrail — it is REJECTED
    and never executed. ``reason`` is a short, safe explanation for the typed
    ``GUARDRAIL_BLOCKED`` warning (§21)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedSql:
    """A SQL string that passed the guardrail: the parsed ``SELECT`` AST and the
    single bare table it reads. The executor needs both — the AST to compose the
    build-scoped CTE around, the table name to reconstruct."""

    statement: exp.Select
    table: str


def validate_sql(
    sql: str, allowed_tables: Collection[str], blocked_keywords: Collection[str]
) -> ValidatedSql:
    """Validate ``sql`` against the §21/§27.6 guardrail or raise
    :class:`GuardrailBlocked`. Purely syntactic — no DB access — so it is the
    same check whether the SQL came from the LLM or a test.
    """
    statement = _parse_single(sql)
    # read-only: the one statement must be a SELECT. This alone rejects every
    # DDL/DML root (INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE) and set-op
    # roots (UNION), since sqlglot parses each to its own non-Select node.
    if not isinstance(statement, exp.Select):
        raise GuardrailBlocked(
            f"only a single SELECT is allowed, got {type(statement).__name__.upper()}"
        )
    for node_type, label in _FORBIDDEN:
        if statement.find(node_type) is not None:
            raise GuardrailBlocked(f"{label} is not allowed in a SQL retrieval query")
    _reject_side_effects(statement)
    if not _is_select_star(statement):
        raise GuardrailBlocked(
            "the projection must be SELECT * — whole source rows are returned so each "
            "is citable by (table, pk); explicit column lists are a future extension"
        )
    table = _single_allowed_table(statement, allowed_tables)
    _reject_blocked_keywords(sql, blocked_keywords)
    return ValidatedSql(statement=statement, table=table)


def _parse_single(sql: str) -> exp.Expr:
    """Parse to exactly one statement, or reject. A parse failure is a rejection
    (§27.6: 解析失敗即拒), and ``a; b`` — the classic ``SELECT … ; DROP …`` — is
    two statements, refused before any single-statement checks run."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=_DIALECT) if s is not None]
    except sqlglot.errors.SqlglotError as exc:
        raise GuardrailBlocked(f"unparseable SQL ({type(exc).__name__})") from exc
    if len(statements) != 1:
        raise GuardrailBlocked(f"exactly one statement is allowed, parsed {len(statements)}")
    return statements[0]


def _reject_side_effects(statement: exp.Select) -> None:
    """Reject reads that can still mutate state or escape a pure SELECT — the
    structural checks above do not catch these (§21 read-only). A function call
    may take an advisory lock, change a setting, or advance a sequence
    (``pg_advisory_lock``, ``set_config``, ``nextval``), which the deferred
    read-only role would NOT stop; so only a type CAST is allowed and every other
    function is refused (``exp.Cast`` is itself an ``exp.Func`` subclass, hence the
    isinstance skip). ``FOR UPDATE``/``FOR SHARE`` takes row locks; ``SELECT INTO``
    writes a new table — both refused."""
    for func in statement.find_all(exp.Func):
        if not isinstance(func, exp.Cast):
            rendered = func.sql(dialect=_DIALECT)
            raise GuardrailBlocked(
                f"a function call ({rendered[:40]}) is not allowed — a function may take a lock, "
                "change settings, be non-deterministic, or fold rows; only casts are permitted"
            )
    if statement.args.get("locks"):
        raise GuardrailBlocked(
            "row locking (FOR UPDATE / FOR SHARE) is not allowed (§21 read-only)"
        )
    if statement.args.get("into") is not None:
        raise GuardrailBlocked("SELECT INTO is not allowed — it writes a new table (§21 read-only)")


def _is_select_star(statement: exp.Select) -> bool:
    """True iff the projection is exactly ``*`` (a single unqualified star)."""
    projections = statement.expressions
    return len(projections) == 1 and isinstance(projections[0], exp.Star)


def _single_allowed_table(statement: exp.Select, allowed_tables: Collection[str]) -> str:
    """The one bare table the query reads, if it is whitelisted; else reject.

    Joins/subqueries are already rejected, so a well-formed query has exactly one
    table node. A schema/catalog qualifier (``db``/``catalog``) is refused: the
    executor reconstructs the *bare* name as a build-scoped CTE, and a qualified
    reference would resolve past that CTE to a real base table (scope bypass)."""
    table_nodes = list(statement.find_all(exp.Table))
    if len(table_nodes) != 1:
        names = sorted({t.name for t in table_nodes})
        raise GuardrailBlocked(f"exactly one table is allowed, found {names or 'none'}")
    node = table_nodes[0]
    if node.db or node.catalog:
        raise GuardrailBlocked(
            f"a schema-qualified table name ({node.sql(dialect=_DIALECT)}) is not allowed — "
            "reference the whitelisted table by its bare name"
        )
    # A column-alias list (``FROM t AS a(c1, c2, ...)``) RENAMES the table's columns
    # positionally. The executor swaps the table for a reconstruction CTE whose first
    # columns are the citation fields (``__row_pk``/``__source_uri``); a SELECT * over
    # a column-alias list could rename a DATA column onto ``__row_pk``, so `_to_results`
    # would cite the row by a forged data value, not its real pk. A plain table alias
    # (no column list) is harmless and still allowed.
    alias = node.args.get("alias")
    if alias is not None and alias.columns:
        raise GuardrailBlocked(
            "a column-alias list on the table (FROM t AS a(c1, ...)) is not allowed — "
            "it can rename a data column onto the __row_pk/__source_uri citation fields"
        )
    name = node.name
    if name not in allowed_tables:
        raise GuardrailBlocked(f"table {name!r} is not in the allowed_tables whitelist")
    return name


def _reject_blocked_keywords(sql: str, blocked_keywords: Collection[str]) -> None:
    """Defense in depth (§21): reject if any blocked word appears as a KEYWORD
    token. Runs on the token stream, not the raw string, so a blocked word inside
    a string literal or a quoted identifier is not a false positive."""
    blocked = {word.lower() for word in blocked_keywords}
    for token in sqlglot.tokenize(sql):
        if token.token_type in _NON_KEYWORD_TOKENS:
            continue
        if token.text.lower() in blocked:
            raise GuardrailBlocked(f"blocked keyword {token.text.lower()!r} is present")
