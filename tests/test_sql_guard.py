"""Why: the SQL guardrail is the security boundary for NL→SQL (§21/§27.6). An
LLM writes the SQL, so the guardrail is what stands between a natural-language
question and the database — if it lets through a write, a second statement, a
join/aggregate that can't be cited, or a schema-qualified name that dodges the
build-scoped CTE, the whole read-only/citable guarantee is gone. These tests
pin BOTH halves the retro's guard checklist demands: every dangerous/uncitable
construct is REJECTED (reject-surface completeness), AND legitimate flat
single-table reads are ACCEPTED (the over-block dual — an attack-only test set
is false-green against a guardrail that rejects everything).
"""

from __future__ import annotations

import pytest

from core.query.policy import SQL_BLOCKED_KEYWORDS_MIN
from core.query.sql_guard import GuardrailBlocked, ValidatedSql, validate_sql

_ALLOWED = ("orders", "customers")
_BLOCKED = SQL_BLOCKED_KEYWORDS_MIN


def _validate(
    sql: str, *, allowed: tuple[str, ...] = _ALLOWED, blocked: tuple[str, ...] = _BLOCKED
) -> ValidatedSql:
    return validate_sql(sql, allowed, blocked)


# --- ACCEPT: legitimate flat single-table reads (the over-block dual) ---------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT * FROM orders WHERE amount = '5'",
        "SELECT * FROM orders WHERE amount::numeric > 100",  # a `::` cast is read-only
        "SELECT * FROM orders WHERE CAST(amount AS numeric) > 100",  # the CAST() form too
        "SELECT * FROM orders WHERE customer = 'acme' ORDER BY amount LIMIT 10",
        "SELECT * FROM orders ORDER BY amount::numeric DESC",  # a named/cast sort key is fine
        "SELECT * FROM orders ORDER BY (amount) DESC",  # a parenthesized EXPRESSION, not an ordinal
        "select * from customers",  # lowercase keywords
        "SELECT * FROM orders WHERE note = 'please delete this order'",  # blocked word in a STRING
        'SELECT * FROM orders WHERE "select" = 1',  # blocked word as a QUOTED identifier
        "SELECT * FROM orders o WHERE o.amount = '5'",  # a PLAIN table alias is harmless
    ],
)
def test_accepts_flat_single_table_reads(sql: str) -> None:
    """A flat ``SELECT *`` over one whitelisted table — with WHERE/ORDER/LIMIT,
    casts, and blocked words that appear only as data (string literal or quoted
    identifier) — must pass. Rejecting these would be over-blocking: the guard
    would refuse legitimate retrieval, not just attacks."""
    validated = _validate(sql)
    assert validated.table in _ALLOWED


def test_accept_reports_the_single_table_read() -> None:
    """The validated result names the one table, so the executor knows which
    logical relation to reconstruct."""
    assert _validate("SELECT * FROM orders WHERE x = '1'").table == "orders"


# --- REJECT: writes / DDL (read-only, §27.6 禁 DDL/DML) ------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET amount = '0'",
        "INSERT INTO orders VALUES ('1')",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN x int",
        "TRUNCATE orders",
        "CREATE TABLE t (id int)",
    ],
)
def test_rejects_writes_and_ddl(sql: str) -> None:
    """Only a SELECT is read-only. Every DML/DDL verb parses to its own
    non-SELECT root and is refused before it can touch the database."""
    with pytest.raises(GuardrailBlocked):
        _validate(sql)


# --- REJECT: uncitable constructs (§16/§27.2 require_sources) ------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders JOIN customers ON orders.cid = customers.id",  # join
        "SELECT * FROM orders WHERE amount > (SELECT avg(amount) FROM orders)",  # subquery
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t",  # CTE
        "SELECT * FROM orders UNION SELECT * FROM customers",  # set op
        "SELECT * FROM orders GROUP BY customer",  # group by
        "SELECT count(*) FROM orders",  # aggregate (also not star)
        "SELECT DISTINCT customer FROM orders",  # distinct (also not star)
        "SELECT * FROM orders TABLESAMPLE SYSTEM (1)",  # samples a subset BEFORE WHERE
        "SELECT * FROM orders TABLESAMPLE BERNOULLI (10)",  # any sampling method
        "SELECT * FROM orders ORDER BY amount OFFSET 10",  # OFFSET silently skips rows
        "SELECT * FROM orders LIMIT 5 OFFSET 10",  # OFFSET even alongside a LIMIT
    ],
)
def test_rejects_uncitable_constructs(sql: str) -> None:
    """A join/subquery/CTE/set-op/GROUP BY/aggregate/DISTINCT produces rows that
    fold many source rows or none — no single (table, pk) can cite them, so the
    §27.2 require_sources contract can't be met. Rejected in v1."""
    with pytest.raises(GuardrailBlocked):
        _validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders ORDER BY 1",  # ordinal → sorts by the hidden __row_pk
        "SELECT * FROM orders ORDER BY 2 DESC",  # ordinal → sorts by __source_uri
        "SELECT * FROM orders ORDER BY amount DESC, 1",  # one positional term among named
        "SELECT * FROM orders ORDER BY (1)",  # PG honours a parenthesized ordinal too
        "SELECT * FROM orders ORDER BY (2) DESC",  # …with a direction
        "SELECT * FROM orders ORDER BY ((1))",  # …and nested parens
    ],
)
def test_rejects_a_positional_order_by_that_sorts_by_hidden_citation_fields(sql: str) -> None:
    """A positional ORDER BY sorts by output-column ordinal, but the reconstruction
    prepends __row_pk/__source_uri before the data columns — so ORDER BY 1/2 would
    silently sort by a hidden citation field, not the column the schema prompt
    shows. Rejected; the model must order by a column name (§21 over-block)."""
    with pytest.raises(GuardrailBlocked, match="positional ORDER BY"):
        _validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders LIMIT 2::int",  # a cast — passes _reject_side_effects
        "SELECT * FROM orders LIMIT CAST(2 AS int)",  # the CAST() spelling
        "SELECT * FROM orders LIMIT 1+1",  # a computed limit
        "SELECT * FROM orders LIMIT 'x'",  # a string literal — not int-parseable
        "SELECT * FROM orders LIMIT 2.5",  # a float literal — non-string but int() crashes
        "SELECT * FROM orders LIMIT 1e3",  # scientific notation — same trap
    ],
)
def test_rejects_a_nonliteral_limit_that_would_bypass_the_row_cap(sql: str) -> None:
    """The row cap reads the LIMIT as a plain integer literal; a casted/computed
    LIMIT is not a literal, so the cap falls through to the policy max_rows and the
    query would silently return MORE rows than it asked for. Rejected, not
    over-returned (§21 over-block)."""
    with pytest.raises(GuardrailBlocked, match="LIMIT must be a plain integer literal"):
        _validate(sql)


# --- REJECT: side-effecting reads (§21 read-only — a SELECT can still mutate) --


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders WHERE pg_advisory_lock(42) IS NULL",  # session-level lock
        "SELECT * FROM orders WHERE pg_advisory_xact_lock(1) IS NULL",  # txn lock
        "SELECT * FROM orders WHERE set_config('work_mem', '1GB', false) IS NULL",  # setting
        "SELECT * FROM orders WHERE nextval('s') > 0",  # advances a sequence
        "SELECT * FROM orders WHERE pg_sleep(5) IS NULL",  # DoS / resource hold
        "SELECT * FROM orders WHERE lower(customer) = 'acme'",  # any non-cast function
        "SELECT * FROM orders FOR UPDATE",  # row locks
        "SELECT * FROM orders FOR SHARE",  # row share locks
        "SELECT * INTO evil FROM orders",  # writes a new table
    ],
)
def test_rejects_side_effecting_reads(sql: str) -> None:
    """A SELECT is not automatically side-effect-free: a function can take an
    advisory lock, change a setting, or advance a sequence; FOR UPDATE/SHARE takes
    row locks; SELECT INTO writes a table. None are stopped by the read-only role,
    so the guardrail refuses every non-cast function, any lock clause, and INTO —
    only a type cast survives (§21 read-only)."""
    with pytest.raises(GuardrailBlocked):
        _validate(sql)


# --- REJECT: shape / scope violations -----------------------------------------


def test_rejects_a_second_statement() -> None:
    """The classic ``SELECT … ; DROP …`` — two statements — is refused; only one
    statement is allowed, so a piggy-backed write can't ride along."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM orders; DROP TABLE orders")


def test_rejects_unparseable_sql() -> None:
    """§27.6: a parse failure is a rejection, never a best-effort execution."""
    with pytest.raises(GuardrailBlocked):
        _validate("not sql at all !!!")


def test_rejects_explicit_column_projection() -> None:
    """v1 returns whole rows (SELECT *) so each is citable by (table, pk); an
    explicit column list is refused rather than silently returning uncitable
    partial rows."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT customer, amount FROM orders")


def test_rejects_a_table_not_in_the_whitelist() -> None:
    """A table outside allowed_tables is refused — the whole point of the
    whitelist (§21)."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM secrets")


def test_rejects_an_implicit_comma_join() -> None:
    """A comma cross-join (``FROM a, b``) still names two tables — refused (it
    parses as a JOIN, but the single-table check is the backstop regardless)."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM orders, customers")


def test_rejects_a_query_with_no_table() -> None:
    """``SELECT *`` with no FROM reads no whitelisted table at all — there is
    nothing to reconstruct or cite, so it is refused (the zero-table backstop)."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT *")


def test_rejects_a_schema_qualified_table() -> None:
    """A schema/catalog-qualified name would resolve PAST the build-scoped CTE
    (named by the bare table) to a real base table — a scope bypass. Even when
    the bare name is whitelisted, the qualified form is refused."""
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM public.orders")
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM pg_catalog.pg_tables")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders AS o(a, b, __row_pk)",  # renames a data column onto the pk
        "SELECT * FROM orders o(c1, __source_uri)",  # forges the source_uri citation field
        "SELECT * FROM orders AS o(id)",  # any column-alias list at all
    ],
)
def test_rejects_a_column_alias_list_that_could_forge_citations(sql: str) -> None:
    """A column-alias list (``FROM t AS a(c1, ...)``) renames the table's columns
    positionally. Since the executor swaps in a reconstruction CTE whose leading
    columns are the citation fields, a SELECT * over such a list could project a
    DATA value under ``__row_pk``/``__source_uri`` — so the row would be cited by a
    forged value, not its real pk (§27.2). The bare column-alias list is refused; a
    plain table alias (no list) stays allowed (see the accept cases)."""
    with pytest.raises(GuardrailBlocked, match="column-alias list"):
        _validate(sql)


# --- Blocked-keyword defense in depth (on top of the AST) ---------------------


def test_blocked_keyword_rejects_a_matching_token_but_not_a_string_literal() -> None:
    """The keyword list is defense in depth (§21): a project may extend it, and a
    match on a real KEYWORD/identifier token is rejected — but the same word
    inside a STRING literal is data, not a verb, and must pass (no over-block).
    Here a project blocks the ``orders`` table name; a query naming it as a token
    is refused, while one mentioning it only in a string is allowed."""
    blocked = (*_BLOCKED, "orders")
    with pytest.raises(GuardrailBlocked):
        _validate("SELECT * FROM orders", blocked=blocked)
    # 'orders' only inside a string literal → not a token match → allowed
    assert (
        _validate("SELECT * FROM customers WHERE note = 'these are orders'", blocked=blocked).table
        == "customers"
    )
